"""Turn per-word highlight boxes into human-readable change descriptions.

Shared by the inspector panel and the report writers so both phrase a change
the same way.  Qt-free on purpose.

A rewritten line produces one highlight box per word.  Listing those verbatim
("Replaced text: commodo → THIS", "Replaced text: minim → LINE", …) buries the
reader in fragments and, because replacements are recorded on both pages, shows
every entry twice.  This module stitches adjacent same-category words back into
the phrase they came from and drops the duplicate side.
"""
from __future__ import annotations

from .models import CH_CHANGED, CH_FORMAT, SIDE_A

# Words are "on the same line" within this vertical slack, and "adjacent" if
# the horizontal gap is no wider than a generous inter-word space.
SAME_LINE_PT = 2.5
WORD_GAP_PT = 24.0


class Change:
    """One described change, anchored to a highlight box for navigation."""

    __slots__ = ("category", "text", "anchor")

    def __init__(self, category: str, text: str, anchor):
        self.category = category
        self.text = text
        self.anchor = anchor

    def __repr__(self):
        return f"Change({self.category!r}, {self.text!r})"


def group_changes(boxes) -> list[Change]:
    """Collapse highlight boxes into phrase-level changes."""
    by_side: dict[int, list] = {}
    for box in boxes or ():
        by_side.setdefault(box[1], []).append(box)

    runs: list[Change] = []
    for side in sorted(by_side):
        runs.extend(_group_one_side(by_side[side], side))

    seen: set = set()
    unique: list[Change] = []
    for change in runs:
        key = (change.category, change.text)
        if key in seen:
            continue
        seen.add(key)
        unique.append(change)
    return unique


def _group_one_side(boxes, side: int) -> list[Change]:
    runs: list[Change] = []
    cur_cat = None
    befores: list[str] = []
    afters: list[str] = []
    anchor = None
    last_y = last_x1 = None

    def flush():
        nonlocal cur_cat, befores, afters, anchor, last_y, last_x1
        if cur_cat and befores:
            text = (f"{' '.join(befores)} → {' '.join(afters)}" if afters
                    else " ".join(befores))
            runs.append(Change(cur_cat, text, anchor))
        cur_cat, befores, afters, anchor = None, [], [], None
        last_y = last_x1 = None

    for box in boxes:
        category, _side, x0, y0, x1, y1, text, detail = box

        if category == CH_CHANGED:
            # The B-side copy describes the same substitution as the A-side
            # one, so only the A side is walked.
            if side != SIDE_A:
                continue
            before, _, after = (detail or "").partition(" → ")
            before = before or text
        elif category == CH_FORMAT:
            flush()
            runs.append(Change(category,
                               f"{text}  ({detail})" if detail else text, box))
            continue
        elif detail and not text:
            # Image moves and similar carry their own standalone description.
            flush()
            runs.append(Change(category, detail, box))
            continue
        elif text:
            before, after = text, ""
        else:
            continue

        same_line = last_y is not None and abs(y0 - last_y) <= SAME_LINE_PT
        adjacent = last_x1 is not None and -2.0 <= x0 - last_x1 <= WORD_GAP_PT
        if category == cur_cat and same_line and adjacent:
            befores.append(before)
            if after:
                afters.append(after)
        else:
            flush()
            cur_cat, anchor = category, box
            befores = [before]
            afters = [after] if after else []
        last_y, last_x1 = y0, x1

    flush()
    return runs
