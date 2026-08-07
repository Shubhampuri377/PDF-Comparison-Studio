"""Accuracy checks against the ways real PDFs differ without differing.

The synthetic fixtures elsewhere are produced by one code path, so both sides
tokenise identically and every accuracy bug hides. Real documents come from
different producers -- Word, InDesign, a print driver, a re-save -- and encode
the same visible text differently. These cases reproduce that.

Non-ASCII characters are written as escapes on purpose. Literal ligatures and
dashes in a test file are one careless re-encode away from silently becoming
something else, at which point the test no longer checks what it claims to.

    python -m tests.test_accuracy
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from pdfdiff.engine import ComparisonEngine                    # noqa: E402
from pdfdiff.models import (CH_ADDED, CH_CHANGED, CH_REMOVED,  # noqa: E402
                            CompareSettings, RuntimeSettings)

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")


def _write(path: str, lines: list[str], *, reverse_blocks: bool = False):
    """One line per text block, so block order is controllable."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    order = list(range(len(lines)))
    if reverse_blocks:
        order.reverse()
    for i in order:
        page.insert_text((60, 80 + i * 22), lines[i], fontsize=11)
    doc.save(path)
    doc.close()


def _compare(a: str, b: str, settings: CompareSettings | None = None):
    eng = ComparisonEngine(a, b, settings=settings or CompareSettings(),
                           runtime=RuntimeSettings(workers=2))
    summary = eng.run()
    eng.close()
    return summary


def _counts(summary):
    return (summary.counts.get(CH_REMOVED, 0),
            summary.counts.get(CH_ADDED, 0),
            summary.counts.get(CH_CHANGED, 0))


BODY = [
    "The quick brown fox jumps over the lazy dog",
    "Financial results for the first quarter of the year",
    "Office efficiency improved by a significant margin",
    "Definitive agreement signed by both parties involved",
    "Sufficient staffing was confirmed for the final phase",
]


def test_identical_text():
    print("\nbaseline: byte-identical documents")
    os.makedirs(FIXTURES, exist_ok=True)
    a = os.path.join(FIXTURES, "acc_same_a.pdf")
    b = os.path.join(FIXTURES, "acc_same_b.pdf")
    _write(a, BODY)
    _write(b, BODY)
    s = _compare(a, b)
    r, ad, ch = _counts(s)
    check("no differences reported", (r, ad, ch) == (0, 0, 0),
          f"removed={r} added={ad} changed={ch}")


def test_normaliser_folds_lookalikes():
    """Characters that render identically must tokenise identically.

    Tested at the normaliser rather than through a PDF: the base-14 fonts have
    no ligature glyph, so writing U+FB01 into a test PDF silently substitutes a
    middle dot and the file no longer contains what the test intended.
    """
    print("\nnormalisation of visually identical characters")
    from pdfdiff.diffcore import TextNormaliser

    n = TextNormaliser(CompareSettings())
    cases = [
        ("ligature ffi", "oﬃce", "office"),
        ("ligature fi", "ﬁle", "file"),
        ("ligature fl", "ﬂow", "flow"),
        ("non-breaking space", "25 000", "25 000"),
        ("narrow no-break space", "25 000", "25 000"),
        ("soft hyphen", "well­known", "wellknown"),
        ("zero-width space", "in​voice", "invoice"),
        ("curly apostrophe", "company’s", "company's"),
        ("curly quotes", "“Acme”", '"Acme"'),
        ("en dash", "2020–2021", "2020-2021"),
        ("em dash", "a—b", "a-b"),
        ("non-breaking hyphen", "well‑known", "well-known"),
        ("minus sign", "−5", "-5"),
        ("fullwidth digits", "１２３", "123"),
        ("ellipsis", "wait…", "wait..."),
        ("BOM in the middle", "in﻿voice", "invoice"),
    ]
    for label, left, right in cases:
        check(f"{label}", n.token(left) == n.token(right),
              f"{n.token(left)!r} vs {n.token(right)!r}")

    # Folding must not go so far that real edits disappear.
    check("a real difference survives normalisation",
          n.token("increase") != n.token("decrease"))
    check("digits are not folded by default",
          n.token("1,200") != n.token("1,300"))
    check("case is not folded by default",
          n.token("Total") != n.token("total"))


def test_block_order():
    """Same visible page, blocks emitted in a different order."""
    print("\nsame layout, different content-stream block order")
    a = os.path.join(FIXTURES, "acc_ord_a.pdf")
    b = os.path.join(FIXTURES, "acc_ord_b.pdf")
    _write(a, BODY, reverse_blocks=False)
    _write(b, BODY, reverse_blocks=True)
    s = _compare(a, b)
    r, ad, ch = _counts(s)
    check("block order does not create differences", (r, ad, ch) == (0, 0, 0),
          f"removed={r} added={ad} changed={ch}")


def _image_pdf(path: str, quality: int, tint: tuple[int, int, int] = (40, 90, 170)):
    import io

    from PIL import Image

    base = Image.new("RGB", (240, 160), tint)
    for x in range(240):                      # horizontal stripes: a plain
        for y in range(0, 160, 8):            # left-right difference hash
            base.putpixel((x, y), (250, 250, 250))   # collapses to zero here
    buf = io.BytesIO()
    base.save(buf, format="JPEG", quality=quality)

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((60, 80), "Report with a logo", fontsize=12)
    page.insert_image(fitz.Rect(60, 120, 300, 280), stream=buf.getvalue())
    doc.save(path)
    doc.close()


def test_same_image_recompressed():
    print("\nidentical image, re-encoded at a different quality")
    a = os.path.join(FIXTURES, "acc_img_a.pdf")
    b = os.path.join(FIXTURES, "acc_img_b.pdf")
    _image_pdf(a, 95)
    _image_pdf(b, 78)
    s = _compare(a, b)
    img = sum(v for k, v in s.counts.items() if k.startswith("img_"))
    check("re-encoded image is not reported as changed", img == 0,
          f"image changes={img} counts={s.counts}")


def test_genuinely_different_image():
    """The image fix must not make every picture match every other."""
    print("\ncontrol: a genuinely different image")
    a = os.path.join(FIXTURES, "acc_img2_a.pdf")
    b = os.path.join(FIXTURES, "acc_img2_b.pdf")
    _image_pdf(a, 92, tint=(40, 90, 170))
    _image_pdf(b, 92, tint=(200, 60, 40))     # same shape, very different hue
    s = _compare(a, b)
    img = sum(v for k, v in s.counts.items() if k.startswith("img_"))
    check("a different image is still reported", img > 0,
          f"image changes={img} counts={s.counts}")


def test_real_edit_still_detected():
    """The fixes must not blind the tool to genuine changes."""
    print("\ncontrol: a genuine text edit")
    a = os.path.join(FIXTURES, "acc_edit_a.pdf")
    b = os.path.join(FIXTURES, "acc_edit_b.pdf")
    edited = list(BODY)
    edited[2] = "Office efficiency DECLINED by a significant margin"
    _write(a, BODY)
    _write(b, edited)
    s = _compare(a, b)
    r, ad, ch = _counts(s)
    check("the real edit is reported", (r + ad + ch) > 0,
          f"removed={r} added={ad} changed={ch}")
    check("and nothing else is", (r + ad + ch) <= 4,
          f"expected ~1 word, got removed={r} added={ad} changed={ch}")


def test_font_subset_tags():
    """Re-saving a PDF regenerates every embedded font's subset tag."""
    print("\nembedded font subset tags")
    from pdfdiff.extract import normalise_font_name as fold

    check("same family, different subset tag",
          fold("ABCDEE+Calibri") == fold("GHIJKL+Calibri"))
    check("tagged matches untagged",
          fold("AAAAAA+Helvetica") == fold("Helvetica"))
    check("separator noise folded",
          fold("Arial-BoldMT") == fold("ArialBoldMT"))
    check("different families still differ",
          fold("ABCDEF+Calibri") != fold("ABCDEF+Arial"))
    check("weight is still a difference",
          fold("Arial") != fold("Arial-Bold"))


def test_rotated_page_highlights():
    """Word boxes must land on the page as rendered, not as stored."""
    print("\nrotated pages")
    from pdfdiff.extract import ingest_chunk
    from pdfdiff.store import unpack

    for rot in (0, 90, 180, 270):
        # A distinct file per rotation: ingest keeps parsed documents open in
        # a per-process cache, so rewriting one path in a loop fails on
        # Windows with a sharing violation.
        path = os.path.join(FIXTURES, f"acc_rot_{rot}.pdf")
        doc = fitz.open()
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 100), "TOP LEFT MARKER", fontsize=14)
        if rot:
            page.set_rotation(rot)
        doc.save(path)
        doc.close()

        res = ingest_chunk({"path": path, "side": 0, "start": 0, "end": 1,
                            "compare_images": False, "image_fuzzy": False,
                            "compare_formatting": False})
        row = res["rows"][0]
        width, height = row[2], row[3]
        words = (unpack(row[9]) or {}).get("w") or []
        inside = all(-1 <= w[0] <= width + 1 and -1 <= w[1] <= height + 1
                     for w in words)
        check(f"rotation {rot}: boxes fall inside the {width:.0f}x{height:.0f} page",
              bool(words) and inside,
              f"{len(words)} words, first={words[0][:4] if words else None}")



def test_positional_drift():
    """Sub-point drift must not masquerade as moved text.

    Re-saving a document rounds glyph advance widths differently, so the
    error accumulates along each line. This is the single biggest source of
    false positives on real files -- it made move detection unusable.
    """
    print("\nsub-point positional drift")
    import random

    from pdfdiff.diffcore import diff_words
    from pdfdiff.models import CH_MOVED

    words = ("The parties hereby agree that the total consideration payable "
             "under this clause shall not exceed the stated maximum").split()
    rng = random.Random(4)
    on = CompareSettings(detect_moves=True)

    def build(drift, jitter, line_offset):
        a, b = [], []
        for line in range(25):
            y = 80 + line * 20
            off = rng.uniform(-line_offset, line_offset) if line_offset else 0.0
            x = 60.0
            for i, t in enumerate(words):
                a.append((round(x, 3), y, round(x + 30, 3), y + 11, t, -1))
                bx = x + off + drift * i
                if jitter:
                    bx += rng.uniform(-jitter, jitter)
                b.append((round(bx, 3), y, round(bx + 30, 3), y + 11, t, -1))
                x += 34
        return a, b

    def moved(a, b):
        c, _bx, _s, _f = diff_words(a, [w[4] for w in a], b,
                                    [w[4] for w in b], [], [], on)
        return c.get(CH_MOVED, 0)

    for label, d, j, o in (("uniform re-save drift", 0.30, 0.0, 0.0),
                           ("heavier drift", 0.60, 0.0, 0.0),
                           ("drift plus per-line offset", 0.30, 0.0, 2.5),
                           ("drift, offset and jitter", 0.30, 0.4, 2.5)):
        a, b = build(d, j, o)
        n = moved(a, b)
        check(f"no false moves: {label}", n == 0, f"{n} reported")

    # A real relocation must survive, and must not drag its line-mates in.
    a, b = build(0.30, 0.0, 2.5)
    w = b[60]
    b[60] = (w[0] + 40, w[1] + 24, w[2] + 40, w[3] + 24, w[4], -1)
    n = moved(a, b)
    check("one relocated word reported exactly once", n == 2,
          f"{n} highlights (2 == one word, both sides)")

    a, b = build(0.30, 0.0, 2.5)
    for idx in (60, 61, 140):
        w = b[idx]
        b[idx] = (w[0] + 40, w[1] + 24, w[2] + 40, w[3] + 24, w[4], -1)
    n = moved(a, b)
    check("three relocated words reported exactly", n == 6, f"{n} highlights")

    check("moves are off by default",
          CompareSettings().detect_moves is False)


def main() -> int:
    import shutil

    from pdfdiff.engine import session_root

    shutil.rmtree(session_root(), ignore_errors=True)
    os.makedirs(FIXTURES, exist_ok=True)

    test_identical_text()
    test_normaliser_folds_lookalikes()
    test_block_order()
    test_font_subset_tags()
    test_rotated_page_highlights()
    test_positional_drift()
    test_same_image_recompressed()
    test_genuinely_different_image()
    test_real_edit_still_detected()

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
