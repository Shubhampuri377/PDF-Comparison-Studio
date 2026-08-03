"""Ignore rules: rectangular regions, page ranges and text patterns.

Regions are stored as *fractions* of the page box (0.0 - 1.0) rather than
absolute points.  Large document sets routinely mix A4, Letter and the odd
landscape exhibit; a fractional rectangle drawn on page 1 still lands on the
same visual area of a differently sized page 4000.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, asdict, field

SCOPE_ALL = "all"
SCOPE_PAGES = "pages"
SCOPE_ODD = "odd"
SCOPE_EVEN = "even"

SIDE_BOTH = "both"
SIDE_LEFT = "left"
SIDE_RIGHT = "right"

_RANGE_RE = re.compile(r"^\s*(\d+)?\s*(?:-\s*(\d+)?)?\s*$")


def parse_page_spec(spec: str) -> list[tuple[int, int]]:
    """Parse ``"1-5, 9, 200-"`` into inclusive 0-based ranges.

    An open upper bound becomes ``(start, -1)``.  Junk segments are skipped
    rather than raising, so a half-typed spec in the UI degrades gracefully.
    """
    ranges: list[tuple[int, int]] = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        m = _RANGE_RE.match(part)
        if not m:
            continue
        lo_s, hi_s = m.group(1), m.group(2)
        if lo_s is None and hi_s is None:
            continue
        if "-" not in part:
            n = int(lo_s) - 1
            if n >= 0:
                ranges.append((n, n))
            continue
        lo = int(lo_s) - 1 if lo_s else 0
        hi = int(hi_s) - 1 if hi_s else -1
        if lo < 0:
            lo = 0
        if hi != -1 and hi < lo:
            lo, hi = hi, lo
        ranges.append((lo, hi))
    return ranges


def spec_matches(ranges: list[tuple[int, int]], pno: int) -> bool:
    for lo, hi in ranges:
        if hi == -1:
            if pno >= lo:
                return True
        elif lo <= pno <= hi:
            return True
    return False


@dataclass
class IgnoreRule:
    """A rectangular exclusion zone expressed in page fractions."""

    x0: float = 0.0
    y0: float = 0.0
    x1: float = 1.0
    y1: float = 1.0
    scope: str = SCOPE_ALL
    pages: str = ""
    side: str = SIDE_BOTH
    label: str = ""
    enabled: bool = True

    def __post_init__(self):
        # Normalise so a rectangle dragged bottom-right to top-left still works.
        if self.x1 < self.x0:
            self.x0, self.x1 = self.x1, self.x0
        if self.y1 < self.y0:
            self.y0, self.y1 = self.y1, self.y0
        self.x0 = max(0.0, min(1.0, self.x0))
        self.y0 = max(0.0, min(1.0, self.y0))
        self.x1 = max(0.0, min(1.0, self.x1))
        self.y1 = max(0.0, min(1.0, self.y1))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "IgnoreRule":
        known = {"x0", "y0", "x1", "y1", "scope", "pages",
                 "side", "label", "enabled"}
        return cls(**{k: v for k, v in (d or {}).items() if k in known})

    def display_name(self) -> str:
        if self.label:
            return self.label
        w = round((self.x1 - self.x0) * 100)
        h = round((self.y1 - self.y0) * 100)
        return f"Region {w}%×{h}%"

    def scope_text(self) -> str:
        if self.scope == SCOPE_ALL:
            base = "All pages"
        elif self.scope == SCOPE_ODD:
            base = "Odd pages"
        elif self.scope == SCOPE_EVEN:
            base = "Even pages"
        else:
            base = f"Pages {self.pages}" if self.pages else "No pages"
        if self.side != SIDE_BOTH:
            base += f" ({self.side})"
        return base


class IgnoreSet:
    """Compiled, hot-path-friendly view of a list of :class:`IgnoreRule`.

    Built once per worker chunk and then queried per word, so everything that
    can be precomputed is precomputed.
    """

    __slots__ = ("_by_side", "_empty")

    def __init__(self, rules: list[IgnoreRule] | list[dict] | None):
        parsed: list[IgnoreRule] = []
        for r in rules or []:
            rule = r if isinstance(r, IgnoreRule) else IgnoreRule.from_dict(r)
            if rule.enabled:
                parsed.append(rule)

        # side index 0 == left/A, 1 == right/B
        self._by_side: tuple[list, list] = ([], [])
        for rule in parsed:
            ranges = parse_page_spec(rule.pages) if rule.scope == SCOPE_PAGES else []
            entry = (rule.x0, rule.y0, rule.x1, rule.y1, rule.scope, ranges)
            if rule.side in (SIDE_BOTH, SIDE_LEFT):
                self._by_side[0].append(entry)
            if rule.side in (SIDE_BOTH, SIDE_RIGHT):
                self._by_side[1].append(entry)
        self._empty = not (self._by_side[0] or self._by_side[1])

    def __bool__(self) -> bool:
        return not self._empty

    def rects_for(self, side: int, pno: int) -> list[tuple[float, float, float, float]]:
        """Fractional rectangles that apply to this page, for the UI overlay."""
        out = []
        for x0, y0, x1, y1, scope, ranges in self._by_side[side]:
            if _scope_hit(scope, ranges, pno):
                out.append((x0, y0, x1, y1))
        return out

    def masker(self, side: int, pno: int, width: float, height: float):
        """Return ``f(x0, y0, x1, y1) -> bool`` telling if a box is ignored.

        Returns ``None`` when no rule applies to this page so callers can skip
        the per-word call entirely -- that check saves real time at 20k pages.
        """
        rects = []
        for x0, y0, x1, y1, scope, ranges in self._by_side[side]:
            if _scope_hit(scope, ranges, pno):
                rects.append((x0 * width, y0 * height, x1 * width, y1 * height))
        if not rects:
            return None

        def is_ignored(bx0: float, by0: float, bx1: float, by1: float) -> bool:
            # A word counts as ignored when its centre falls inside a region.
            # Centre-point beats overlap here: a rectangle drawn tight around a
            # header should not swallow a body word whose box grazes the edge.
            cx = (bx0 + bx1) * 0.5
            cy = (by0 + by1) * 0.5
            for rx0, ry0, rx1, ry1 in rects:
                if rx0 <= cx <= rx1 and ry0 <= cy <= ry1:
                    return True
            return False

        return is_ignored


def _scope_hit(scope: str, ranges: list, pno: int) -> bool:
    if scope == SCOPE_ALL:
        return True
    if scope == SCOPE_ODD:
        return (pno + 1) % 2 == 1
    if scope == SCOPE_EVEN:
        return (pno + 1) % 2 == 0
    return spec_matches(ranges, pno)


# --------------------------------------------------------------------------
# Presets offered in the UI.  These cover the overwhelming majority of
# "why is every page different?" support questions.
# --------------------------------------------------------------------------
PATTERN_PRESETS: list[tuple[str, str]] = [
    ("Page numbers", r"^(?:page\s*)?\d+(?:\s*/\s*\d+)?$"),
    ("Dates (d/m/y, y-m-d)", r"^\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}$"),
    ("Times", r"^\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?$"),
    ("Long numbers / IDs", r"^[A-Z0-9]{8,}$"),
    ("Currency amounts", r"^[$£€₹]\s?[\d,]+(?:\.\d{2})?$"),
    ("Bates / doc stamps", r"^[A-Z]{2,6}[-_ ]?\d{4,}$"),
    ("UUIDs", r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
              r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
]
