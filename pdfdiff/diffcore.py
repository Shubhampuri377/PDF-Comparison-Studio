"""The actual diff: words, formatting and images for one page pair.

Runs inside multiprocessing workers.  Workers read page payloads straight out
of the SQLite store rather than receiving them over a pipe -- shipping 200 MB
of pickled word boxes through IPC would dominate the runtime.
"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from statistics import median

from .ignore import IgnoreSet
from .models import (
    CH_ADDED, CH_CHANGED, CH_FORMAT, CH_IMG_ADDED, CH_IMG_CHANGED,
    CH_IMG_MOVED, CH_IMG_REMOVED, CH_MOVED, CH_REMOVED,
    CompareSettings, FLAGS_KEY, KIND_ADDED, KIND_MATCHED, KIND_REMOVED,
    SIDE_A, SIDE_B, ST_CHANGED, ST_ERROR, ST_IDENTICAL,
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_DIGIT_RE = re.compile(r"\d")
_WS_RE = re.compile(r"\s+")

# If more than this fraction of a page's words look "moved", the page was
# reflowed rather than edited.  Reporting 4,000 move markers helps nobody, so
# the category is suppressed and the page is flagged instead.
_REFLOW_RATIO = 0.60


# Characters that carry no visible meaning but differ between producers:
# soft hyphen, zero-width space/non-joiner/joiner, word joiner, BOM.
_INVISIBLE_RE = re.compile("[­​‌‍⁠﻿]")

# Typographic variants that render identically to a reader. NFKC folds
# ligatures and exotic spaces, but leaves quotes and dashes alone.
_LOOKALIKES = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "′": "'", "″": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-",
    "—": "-", "―": "-", "−": "-",
    "…": "...",
}
_LOOKALIKE_TABLE = {ord(k): v for k, v in _LOOKALIKES.items()}


class TextNormaliser:
    """Compiled once per chunk; called once per word."""

    __slots__ = ("case", "punct", "digits", "ws", "patterns")

    def __init__(self, settings: CompareSettings):
        self.case = settings.ignore_case
        self.punct = settings.ignore_punctuation
        self.digits = settings.ignore_digits
        self.ws = settings.ignore_whitespace
        self.patterns = settings.compiled_patterns()

    def token(self, text: str) -> str:
        """Fold away differences a reader cannot see.

        Unicode folding is unconditional. It used to be skipped unless one of
        the ignore-* options was on, which meant that with default settings
        "ﬁle" and "file" -- the same word, one using the fi ligature that half
        the PDF producers emit -- compared as different. That single omission
        made ordinary documents look heavily edited.
        """
        t = unicodedata.normalize("NFKC", text)
        t = _INVISIBLE_RE.sub("", t)
        t = t.translate(_LOOKALIKE_TABLE)
        if self.ws:
            t = t.strip()
        if self.case:
            t = t.casefold()
        if self.punct:
            t = _PUNCT_RE.sub("", t)
        if self.digits:
            t = _DIGIT_RE.sub("#", t)
        if self.ws:
            t = _WS_RE.sub(" ", t)
        return t

    def is_ignored_text(self, text: str) -> bool:
        if not self.patterns:
            return False
        stripped = text.strip()
        for p in self.patterns:
            if p.search(stripped):
                return True
        return False


def _prepare_side(payload: dict, side: int, pno: int,
                  width: float, height: float,
                  settings: CompareSettings, ignore_set: IgnoreSet,
                  norm: TextNormaliser):
    """Filter and normalise one page's words.

    Returns ``(tokens, words)`` where ``words`` holds the surviving raw tuples
    aligned index-for-index with ``tokens``.
    """
    words = (payload or {}).get("w") or []
    if not words:
        return [], []

    mask = ignore_set.masker(side, pno, width, height) if ignore_set else None
    header_y = settings.header_ignore_pt
    footer_y = height - settings.footer_ignore_pt if settings.footer_ignore_pt else None

    tokens: list[str] = []
    kept: list[tuple] = []
    for w in words:
        x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
        if header_y and y1 <= header_y:
            continue
        if footer_y is not None and y0 >= footer_y:
            continue
        if mask is not None and mask(x0, y0, x1, y1):
            continue
        if norm.is_ignored_text(text):
            continue
        tok = norm.token(text)
        if not tok:
            continue
        tokens.append(tok)
        kept.append(w)
    return tokens, kept


def _median_shift(pairs: list[tuple[tuple, tuple]]) -> tuple[float, float]:
    """Dominant (dx, dy) between matched words.

    A changed top margin shifts every word on the page by the same amount.
    Subtracting the median turns that from "800 moved words" into "no moves",
    which is what a reviewer actually wants to see.
    """
    if not pairs:
        return 0.0, 0.0
    dxs = [b[0] - a[0] for a, b in pairs]
    dys = [b[1] - a[1] for a, b in pairs]
    return median(dxs), median(dys)


def diff_words(words_a: list[tuple], tokens_a: list[str],
               words_b: list[tuple], tokens_b: list[str],
               styles_a: list[tuple], styles_b: list[tuple],
               settings: CompareSettings):
    """Return ``(counts, highlights, similarity, flags)``."""
    counts: dict[str, int] = {}
    highlights: list[tuple] = []
    flags: list[str] = []

    def add(category: str, side: int, w: tuple, detail: str = ""):
        counts[category] = counts.get(category, 0) + 1
        if len(highlights) < settings.max_boxes_per_page:
            highlights.append((category, side, w[0], w[1], w[2], w[3],
                               w[4], detail))

    matcher = SequenceMatcher(None, tokens_a, tokens_b, autojunk=False)
    opcodes = matcher.get_opcodes()

    equal_pairs: list[tuple[tuple, tuple]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(i2 - i1):
                equal_pairs.append((words_a[i1 + k], words_b[j1 + k]))

    mdx, mdy = _median_shift(equal_pairs)
    tol = settings.move_tolerance_pt
    check_format = settings.compare_formatting and (styles_a or styles_b)

    # A non-zero dominant shift means the whole block of content slid -- a
    # margin or template change.  That is worth saying once, rather than
    # painting a "moved" box on every word that stayed put.
    if equal_pairs and (abs(mdx) > tol or abs(mdy) > tol):
        flags.append("layout_shift")

    moved_candidates: list[tuple[tuple, tuple]] = []
    format_changes: list[tuple[tuple, tuple, str]] = []

    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            for k in range(i2 - i1):
                wa = words_a[i1 + k]
                wb = words_b[j1 + k]
                if tol >= 0:
                    dx = (wb[0] - wa[0]) - mdx
                    dy = (wb[1] - wa[1]) - mdy
                    if abs(dx) > tol or abs(dy) > tol:
                        moved_candidates.append((wa, wb))
                if check_format:
                    sa = styles_a[wa[5]] if 0 <= wa[5] < len(styles_a) else None
                    sb = styles_b[wb[5]] if 0 <= wb[5] < len(styles_b) else None
                    if sa is not None and sb is not None and sa != sb:
                        format_changes.append((wa, wb, _describe_style(sa, sb)))
        elif tag == "delete":
            for w in words_a[i1:i2]:
                add(CH_REMOVED, SIDE_A, w)
        elif tag == "insert":
            for w in words_b[j1:j2]:
                add(CH_ADDED, SIDE_B, w)
        elif tag == "replace":
            n_a = i2 - i1
            n_b = j2 - j1
            common = min(n_a, n_b)
            for k in range(common):
                wa = words_a[i1 + k]
                wb = words_b[j1 + k]
                detail = f"{wa[4]} → {wb[4]}"
                add(CH_CHANGED, SIDE_A, wa, detail)
                add(CH_CHANGED, SIDE_B, wb, detail)
            # The old engine zipped these two slices, which silently discarded
            # the tail whenever the block lengths differed.
            for w in words_a[i1 + common:i2]:
                add(CH_REMOVED, SIDE_A, w)
            for w in words_b[j1 + common:j2]:
                add(CH_ADDED, SIDE_B, w)

    n_equal = len(equal_pairs)
    if moved_candidates and n_equal and \
            len(moved_candidates) / n_equal >= _REFLOW_RATIO:
        flags.append("reflow")
    else:
        for wa, wb in moved_candidates:
            add(CH_MOVED, SIDE_A, wa)
            add(CH_MOVED, SIDE_B, wb)

    for wa, wb, detail in format_changes:
        add(CH_FORMAT, SIDE_A, wa, detail)
        add(CH_FORMAT, SIDE_B, wb, detail)

    denom = max(len(tokens_a), len(tokens_b), 1)
    changed_tokens = (counts.get(CH_REMOVED, 0) + counts.get(CH_ADDED, 0)
                      + counts.get(CH_CHANGED, 0) / 2)
    similarity = max(0.0, 1.0 - changed_tokens / denom)
    return counts, highlights, similarity, flags


_STYLE_FLAG_NAMES = [(1 << 0, "superscript"), (1 << 1, "italic"),
                     (1 << 2, "serif"), (1 << 3, "monospace"),
                     (1 << 4, "bold")]


def _describe_style(sa: tuple, sb: tuple) -> str:
    parts = []
    if sa[0] != sb[0]:
        parts.append(f"font {sa[0]} → {sb[0]}")
    if abs(sa[1] - sb[1]) > 0.05:
        parts.append(f"size {sa[1]}pt → {sb[1]}pt")
    if sa[2] != sb[2]:
        parts.append(f"colour #{sa[2]:06x} → #{sb[2]:06x}")
    if sa[3] != sb[3]:
        before = {n for bit, n in _STYLE_FLAG_NAMES if sa[3] & bit}
        after = {n for bit, n in _STYLE_FLAG_NAMES if sb[3] & bit}
        gained = after - before
        lost = before - after
        if gained:
            parts.append("+" + ", ".join(sorted(gained)))
        if lost:
            parts.append("-" + ", ".join(sorted(lost)))
    return "; ".join(parts)


def _looks_the_same(ia: tuple, ib: tuple, settings: CompareSettings) -> bool:
    """Are two differently encoded images visually the same picture?

    Compares perceptual hashes. Returns False when either hash is missing,
    so an image the extractor could not decode is reported rather than
    silently assumed unchanged.
    """
    if not settings.image_fuzzy:
        return False
    pa, pb = ia[5], ib[5]
    if pa is None or pb is None:
        return False          # undecodable, not "a hash that happens to be 0"
    if (pa ^ pb).bit_count() > settings.image_hash_tolerance:
        return False
    # A flat block thresholds to the same all-zero pattern whatever its
    # colour, so overall brightness has to agree as well.
    ma = ia[7] if len(ia) > 7 else None
    mb = ib[7] if len(ib) > 7 else None
    if ma is not None and mb is not None and abs(ma - mb) > 10:
        return False
    return True


def diff_images(images_a: list[tuple], images_b: list[tuple],
                settings: CompareSettings):
    """Match embedded images between two pages.

    Resolution order matters: an image that merely moved should read as
    "moved", not as "removed here, added there".
    """
    counts: dict[str, int] = {}
    highlights: list[tuple] = []

    def add(category: str, side: int, box, detail: str = ""):
        counts[category] = counts.get(category, 0) + 1
        if len(highlights) < settings.max_boxes_per_page:
            highlights.append((category, side, box[0], box[1], box[2], box[3],
                               "", detail))

    remaining_a = list(range(len(images_a)))
    remaining_b = list(range(len(images_b)))
    move_tol = settings.image_move_tolerance_pt

    # Pass 1: identical bytes.  Prefer the nearest placement so a repeated
    # logo pairs with its own copy rather than one across the page.
    by_exact: dict[bytes, list[int]] = {}
    for j in remaining_b:
        key = images_b[j][4]
        if key:
            by_exact.setdefault(key, []).append(j)

    matched: list[tuple[int, int, bool]] = []      # (i, j, exact)
    still_a: list[int] = []
    for i in remaining_a:
        key = images_a[i][4]
        pool = by_exact.get(key) if key else None
        if not pool:
            still_a.append(i)
            continue
        ax, ay = images_a[i][0], images_a[i][1]
        best = min(pool, key=lambda j: abs(images_b[j][0] - ax)
                   + abs(images_b[j][1] - ay))
        pool.remove(best)
        matched.append((i, best, True))
    remaining_b = [j for pool in by_exact.values() for j in pool]

    # Pass 2: same slot, different content -> a genuinely replaced image.
    leftover_a: list[int] = []
    for i in still_a:
        ax0, ay0, ax1, ay1 = images_a[i][:4]
        hit = None
        for j in remaining_b:
            bx0, by0, bx1, by1 = images_b[j][:4]
            if (abs(bx0 - ax0) <= move_tol and abs(by0 - ay0) <= move_tol
                    and abs(bx1 - ax1) <= move_tol and abs(by1 - ay1) <= move_tol):
                hit = j
                break
        if hit is not None:
            remaining_b.remove(hit)
            matched.append((i, hit, False))
        else:
            leftover_a.append(i)

    # Pass 3: perceptually similar but relocated.
    if settings.image_fuzzy and leftover_a and remaining_b:
        tol_bits = settings.image_hash_tolerance
        unresolved_a: list[int] = []
        for i in leftover_a:
            pa = images_a[i][5]
            hit = None
            if pa is not None:
                best_dist = tol_bits + 1
                for j in remaining_b:
                    if not _looks_the_same(images_a[i], images_b[j], settings):
                        continue
                    dist = (pa ^ images_b[j][5]).bit_count()
                    if dist < best_dist:
                        best_dist = dist
                        hit = j
            if hit is not None:
                remaining_b.remove(hit)
                matched.append((i, hit, False))
            else:
                unresolved_a.append(i)
        leftover_a = unresolved_a

    for i, j, exact in matched:
        ia, ib = images_a[i], images_b[j]
        moved = (abs(ib[0] - ia[0]) > move_tol or abs(ib[1] - ia[1]) > move_tol)
        if not exact and not moved:
            # Different bytes in the same slot is not the same as a different
            # picture. Re-saving a PDF re-encodes its images, so comparing raw
            # bytes alone reports every logo in the document as replaced.
            # Only call it changed when it actually looks different.
            if _looks_the_same(ia, ib, settings):
                continue
            add(CH_IMG_CHANGED, SIDE_A, ia)
            add(CH_IMG_CHANGED, SIDE_B, ib)
        elif moved:
            detail = f"moved {ib[0] - ia[0]:+.0f}, {ib[1] - ia[1]:+.0f} pt"
            add(CH_IMG_MOVED, SIDE_A, ia, detail)
            add(CH_IMG_MOVED, SIDE_B, ib, detail)

    for i in leftover_a:
        add(CH_IMG_REMOVED, SIDE_A, images_a[i])
    for j in remaining_b:
        add(CH_IMG_ADDED, SIDE_B, images_b[j])

    return counts, highlights


def diff_pair(a_payload, b_payload, a_geom, b_geom, a_pno, b_pno, kind,
              settings: CompareSettings, ignore_set: IgnoreSet,
              norm: TextNormaliser):
    """Diff one aligned pair.  Returns ``(status, similarity, counts,
    highlights, flags)``."""
    counts: dict[str, int] = {}
    highlights: list[tuple] = []
    flags: list[str] = []

    if kind == KIND_ADDED:
        b_w, b_h = (b_geom[0], b_geom[1]) if b_geom else (612.0, 792.0)
        tokens_b, words_b = _prepare_side(b_payload, SIDE_B, b_pno, b_w, b_h,
                                          settings, ignore_set, norm)
        for w in words_b[:settings.max_boxes_per_page]:
            highlights.append((CH_ADDED, SIDE_B, w[0], w[1], w[2], w[3], w[4], ""))
        counts[CH_ADDED] = len(words_b)
        if settings.compare_images:
            imgs = (b_payload or {}).get("i") or []
            counts[CH_IMG_ADDED] = len(imgs)
            for im in imgs[:64]:
                highlights.append((CH_IMG_ADDED, SIDE_B, im[0], im[1],
                                   im[2], im[3], "", ""))
        return ST_CHANGED, 0.0, counts, highlights, ["page_added"]

    if kind == KIND_REMOVED:
        a_w, a_h = (a_geom[0], a_geom[1]) if a_geom else (612.0, 792.0)
        tokens_a, words_a = _prepare_side(a_payload, SIDE_A, a_pno, a_w, a_h,
                                          settings, ignore_set, norm)
        for w in words_a[:settings.max_boxes_per_page]:
            highlights.append((CH_REMOVED, SIDE_A, w[0], w[1], w[2], w[3], w[4], ""))
        counts[CH_REMOVED] = len(words_a)
        if settings.compare_images:
            imgs = (a_payload or {}).get("i") or []
            counts[CH_IMG_REMOVED] = len(imgs)
            for im in imgs[:64]:
                highlights.append((CH_IMG_REMOVED, SIDE_A, im[0], im[1],
                                   im[2], im[3], "", ""))
        return ST_CHANGED, 0.0, counts, highlights, ["page_removed"]

    a_w, a_h = (a_geom[0], a_geom[1]) if a_geom else (612.0, 792.0)
    b_w, b_h = (b_geom[0], b_geom[1]) if b_geom else (612.0, 792.0)

    tokens_a, words_a = _prepare_side(a_payload, SIDE_A, a_pno, a_w, a_h,
                                      settings, ignore_set, norm)
    tokens_b, words_b = _prepare_side(b_payload, SIDE_B, b_pno, b_w, b_h,
                                      settings, ignore_set, norm)

    styles_a = (a_payload or {}).get("s") or []
    styles_b = (b_payload or {}).get("s") or []

    w_counts, w_boxes, similarity, w_flags = diff_words(
        words_a, tokens_a, words_b, tokens_b, styles_a, styles_b, settings)
    counts.update(w_counts)
    highlights.extend(w_boxes)
    flags.extend(w_flags)

    if settings.compare_images:
        imgs_a = _filter_images((a_payload or {}).get("i") or [],
                                SIDE_A, a_pno, a_w, a_h, settings, ignore_set)
        imgs_b = _filter_images((b_payload or {}).get("i") or [],
                                SIDE_B, b_pno, b_w, b_h, settings, ignore_set)
        i_counts, i_boxes = diff_images(imgs_a, imgs_b, settings)
        for k, v in i_counts.items():
            counts[k] = counts.get(k, 0) + v
        highlights.extend(i_boxes)

    if abs(a_w - b_w) > 1.0 or abs(a_h - b_h) > 1.0:
        flags.append("page_size")

    status = ST_CHANGED if any(counts.values()) else ST_IDENTICAL
    return status, similarity, counts, highlights, flags


def _filter_images(images, side, pno, width, height, settings, ignore_set):
    mask = ignore_set.masker(side, pno, width, height) if ignore_set else None
    if mask is None and not settings.header_ignore_pt \
            and not settings.footer_ignore_pt:
        return images
    header_y = settings.header_ignore_pt
    footer_y = height - settings.footer_ignore_pt if settings.footer_ignore_pt else None
    out = []
    for im in images:
        x0, y0, x1, y1 = im[:4]
        if header_y and y1 <= header_y:
            continue
        if footer_y is not None and y0 >= footer_y:
            continue
        if mask is not None and mask(x0, y0, x1, y1):
            continue
        out.append(im)
    return out


# --------------------------------------------------------------------------
# Worker entry point
# --------------------------------------------------------------------------
def diff_chunk(task: dict) -> dict:
    """Diff a batch of pairs.  ``task`` carries the db path, the settings dict
    and the pair plan rows."""
    from .store import ResultStore, pack
    from .models import ALL_CATEGORIES, TEXT_CATEGORIES, IMAGE_CATEGORIES

    settings = CompareSettings.from_dict(task["settings"])
    ignore_set = IgnoreSet(settings.ignore_rules)
    norm = TextNormaliser(settings)
    plan = task["plan"]

    store = ResultStore(task["db"], readonly=True)
    try:
        keys: list[tuple[int, int]] = []
        for idx, a_pno, b_pno, kind in plan:
            if a_pno is not None:
                keys.append((SIDE_A, a_pno))
            if b_pno is not None:
                keys.append((SIDE_B, b_pno))
        loaded = store.page_payloads(keys)

        rows: list[tuple] = []
        for idx, a_pno, b_pno, kind in plan:
            try:
                a_rec = loaded.get((SIDE_A, a_pno)) if a_pno is not None else None
                b_rec = loaded.get((SIDE_B, b_pno)) if b_pno is not None else None
                a_geom = (a_rec[0], a_rec[1], a_rec[2]) if a_rec else None
                b_geom = (b_rec[0], b_rec[1], b_rec[2]) if b_rec else None
                a_payload = a_rec[3] if a_rec else None
                b_payload = b_rec[3] if b_rec else None

                status, sim, counts, boxes, flags = diff_pair(
                    a_payload, b_payload, a_geom, b_geom,
                    a_pno, b_pno, kind, settings, ignore_set, norm)

                n_text = sum(counts.get(c, 0) for c in TEXT_CATEGORIES)
                n_image = sum(counts.get(c, 0) for c in IMAGE_CATEGORIES)
                n_total = n_text + n_image
                if flags:
                    counts = dict(counts)
                    counts[FLAGS_KEY] = flags
                rows.append((status, sim, n_total, n_text, n_image,
                             pack(counts) if counts else None,
                             pack(boxes) if boxes else None,
                             None, idx))
            except Exception as exc:
                rows.append((ST_ERROR, 0.0, 0, 0, 0, None, None,
                             f"{type(exc).__name__}: {exc}", idx))
        return {"rows": rows}
    finally:
        store.close()
