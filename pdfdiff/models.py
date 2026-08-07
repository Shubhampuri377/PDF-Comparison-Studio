"""Plain data structures shared between the engine, the workers and the UI.

Everything here must stay picklable and free of Qt imports: these objects cross
process boundaries.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict, fields
from typing import Any, Iterable

# Bump whenever the stored page payload or the page-fingerprint algorithm
# changes; cached sessions from an older build are then rejected and re-parsed
# rather than silently answering with stale fingerprints.
SCHEMA_VERSION = 9

# Side identifiers.  Kept as small ints because they are written to SQLite on
# every page row.
SIDE_A = 0
SIDE_B = 1

# Pair kinds -----------------------------------------------------------------
KIND_MATCHED = "matched"   # page exists in both documents
KIND_ADDED = "added"       # page only in document B
KIND_REMOVED = "removed"   # page only in document A

# Pair statuses --------------------------------------------------------------
ST_PENDING = "pending"
ST_IDENTICAL = "identical"
ST_CHANGED = "changed"
ST_ERROR = "error"

# Change categories, also used as highlight layer names -----------------------
CH_REMOVED = "removed"        # text present in A, gone in B
CH_ADDED = "added"            # text new in B
CH_CHANGED = "changed"        # text replaced
CH_MOVED = "moved"            # same text, shifted position
CH_FORMAT = "format"          # same text, different font/size/colour
CH_IMG_REMOVED = "img_removed"
CH_IMG_ADDED = "img_added"
CH_IMG_MOVED = "img_moved"
CH_IMG_CHANGED = "img_changed"

TEXT_CATEGORIES = (CH_REMOVED, CH_ADDED, CH_CHANGED, CH_MOVED, CH_FORMAT)
IMAGE_CATEGORIES = (CH_IMG_REMOVED, CH_IMG_ADDED, CH_IMG_MOVED, CH_IMG_CHANGED)
ALL_CATEGORIES = TEXT_CATEGORIES + IMAGE_CATEGORIES

# Which side a highlight belongs on.  Removals live on the left page, additions
# on the right, everything else is drawn on both.
CATEGORY_SIDES = {
    CH_REMOVED: (SIDE_A,),
    CH_ADDED: (SIDE_B,),
    CH_CHANGED: (SIDE_A, SIDE_B),
    CH_MOVED: (SIDE_A, SIDE_B),
    CH_FORMAT: (SIDE_A, SIDE_B),
    CH_IMG_REMOVED: (SIDE_A,),
    CH_IMG_ADDED: (SIDE_B,),
    CH_IMG_MOVED: (SIDE_A, SIDE_B),
    CH_IMG_CHANGED: (SIDE_A, SIDE_B),
}

CATEGORY_LABELS = {
    CH_REMOVED: "Removed text",
    CH_ADDED: "Added text",
    CH_CHANGED: "Replaced text",
    CH_MOVED: "Moved text",
    CH_FORMAT: "Formatting",
    CH_IMG_REMOVED: "Removed image",
    CH_IMG_ADDED: "Added image",
    CH_IMG_MOVED: "Moved image",
    CH_IMG_CHANGED: "Changed image",
}


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
def _dataclass_from_dict(cls, data: dict):
    """Build a dataclass from a dict, ignoring unknown / missing keys.

    Settings files written by an older build must not crash a newer one.
    """
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in (data or {}).items() if k in known})


@dataclass
class CompareSettings:
    """Everything that influences the *result* of a comparison.

    A hash of these fields is stored alongside the results so the engine knows
    when a cached run can be reused and when it has to re-diff.
    """

    # -- text normalisation ------------------------------------------------
    ignore_case: bool = False
    ignore_whitespace: bool = True
    ignore_punctuation: bool = False
    ignore_digits: bool = False
    ignore_patterns: list[str] = field(default_factory=list)

    # -- geometry ----------------------------------------------------------
    # Off by default. Reviewers overwhelmingly want to know what the text
    # *says*, not that a re-save nudged it; leaving this on made the first
    # thing every user did be to switch it off again.
    detect_moves: bool = False
    move_tolerance_pt: float = 3.0
    header_ignore_pt: float = 0.0
    footer_ignore_pt: float = 0.0

    # -- images ------------------------------------------------------------
    compare_images: bool = True
    image_fuzzy: bool = True
    image_hash_tolerance: int = 6
    image_move_tolerance_pt: float = 4.0

    # -- formatting --------------------------------------------------------
    compare_formatting: bool = False

    # -- page alignment ----------------------------------------------------
    align_pages: bool = True
    align_min_similarity: float = 0.55

    # -- ignore regions ----------------------------------------------------
    ignore_rules: list[dict] = field(default_factory=list)

    # -- limits ------------------------------------------------------------
    max_boxes_per_page: int = 4000

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CompareSettings":
        return _dataclass_from_dict(cls, data)

    def fingerprint(self) -> str:
        """Stable hash of the settings, used for cache invalidation."""
        import hashlib

        blob = json.dumps(self.to_dict(), sort_keys=True, default=str)
        return hashlib.blake2b(blob.encode("utf-8"), digest_size=12).hexdigest()

    def compiled_patterns(self) -> list[re.Pattern]:
        out = []
        for p in self.ignore_patterns:
            if not p or not p.strip():
                continue
            try:
                out.append(re.compile(p))
            except re.error:
                # A malformed user pattern must never abort a 20k page run.
                continue
        return out


@dataclass
class RuntimeSettings:
    """Knobs that affect speed but never the result."""

    dpi: int = 150
    workers: int = 0            # 0 => auto (cpu_count - 1)
    chunk_pages: int = 48
    render_cache_mb: int = 384
    thumbnail_px: int = 132

    def resolved_workers(self) -> int:
        import os

        if self.workers > 0:
            return self.workers
        return max(1, (os.cpu_count() or 2) - 1)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RuntimeSettings":
        return _dataclass_from_dict(cls, data)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
# Non-numeric annotations travel in the same packed blob as the counts, keyed
# with a leading underscore so every aggregation can skip them by prefix.
FLAGS_KEY = "_flags"

FLAG_LABELS = {
    "reflow": "Page reflowed",
    "layout_shift": "Content shifted as a block",
    "page_size": "Page size differs",
    "page_added": "Page added",
    "page_removed": "Page removed",
}


@dataclass
class PageStat:
    """Per-page summary as stored in the ``pair`` table."""

    idx: int
    a_pno: int | None
    b_pno: int | None
    kind: str
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    similarity: float = 1.0
    error: str | None = None
    flags: list[str] = field(default_factory=list)

    def flag_labels(self) -> list[str]:
        return [FLAG_LABELS.get(f, f) for f in self.flags]

    @property
    def total_changes(self) -> int:
        return sum(self.counts.values())

    @property
    def text_changes(self) -> int:
        return sum(self.counts.get(c, 0) for c in TEXT_CATEGORIES)

    @property
    def image_changes(self) -> int:
        return sum(self.counts.get(c, 0) for c in IMAGE_CATEGORIES)

    def label(self) -> str:
        if self.kind == KIND_ADDED:
            return f"+ Page {self.b_pno + 1}"
        if self.kind == KIND_REMOVED:
            return f"- Page {self.a_pno + 1}"
        if self.a_pno == self.b_pno:
            return f"Page {self.a_pno + 1}"
        return f"Page {self.a_pno + 1} â†’ {self.b_pno + 1}"


@dataclass
class Highlight:
    """One rectangle to paint on a rendered page.

    Coordinates are PDF points in the *unrotated* page space, matching what
    PyMuPDF reports for words, so the viewer can scale them by any zoom factor
    without re-running the diff.
    """

    category: str
    side: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str = ""
    detail: str = ""

    def as_tuple(self) -> tuple:
        return (self.category, self.side, self.x0, self.y0,
                self.x1, self.y1, self.text, self.detail)

    @classmethod
    def from_tuple(cls, t: Iterable[Any]) -> "Highlight":
        return cls(*t)


@dataclass
class DocumentInfo:
    path: str
    page_count: int = 0
    title: str = ""
    author: str = ""
    producer: str = ""
    file_size: int = 0
    encrypted: bool = False
    fingerprint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "DocumentInfo":
        return _dataclass_from_dict(cls, data)


@dataclass
class Summary:
    """Whole-run rollup shown on the results header and in reports."""

    total_pairs: int = 0
    identical: int = 0
    changed: int = 0
    added_pages: int = 0
    removed_pages: int = 0
    errors: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    duration_s: float = 0.0
    pages_a: int = 0
    pages_b: int = 0

    @property
    def total_changes(self) -> int:
        return sum(self.counts.values())

    @property
    def match_rate(self) -> float:
        if not self.total_pairs:
            return 1.0
        return self.identical / self.total_pairs

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_changes"] = self.total_changes
        d["match_rate"] = self.match_rate
        return d


@dataclass
class Progress:
    """Emitted from the engine to whatever is driving it."""

    phase: str = ""
    done: int = 0
    total: int = 0
    message: str = ""
    rate: float = 0.0          # units per second
    eta_s: float | None = None

    @property
    def fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.done / self.total)

