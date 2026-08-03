"""Explain why two pages compare as different.

    python tools/why_different.py A.pdf B.pdf [page]

Prints the first token mismatches with their Unicode code points, plus a
per-image comparison. Intended for the case where a comparison reports
differences that are not visible on the page -- it shows exactly what the two
documents encode differently.
"""
from __future__ import annotations

import os
import sys
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from pdfdiff.diffcore import TextNormaliser
from pdfdiff.extract import image_fingerprint, reading_order
from pdfdiff.models import CompareSettings


def codepoints(s: str) -> str:
    return " ".join(f"U+{ord(c):04X}" for c in s)


def describe(s: str) -> str:
    names = []
    for c in s:
        try:
            names.append(unicodedata.name(c))
        except ValueError:
            names.append(f"U+{ord(c):04X}")
    return ", ".join(names)


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    pa, pb = sys.argv[1], sys.argv[2]
    pno = int(sys.argv[3]) - 1 if len(sys.argv) > 3 else 0

    settings = CompareSettings()
    norm = TextNormaliser(settings)

    da, db = fitz.open(pa), fitz.open(pb)
    if pno >= min(da.page_count, db.page_count):
        print(f"page {pno + 1} is out of range")
        return 1

    wa = reading_order(da[pno].get_text("words"))
    wb = reading_order(db[pno].get_text("words"))
    ta = [norm.token(w[4]) for w in wa]
    tb = [norm.token(w[4]) for w in wb]

    print(f"page {pno + 1}:  A has {len(wa)} words, B has {len(wb)}")
    print()

    from difflib import SequenceMatcher
    sm = SequenceMatcher(None, ta, tb, autojunk=False)
    ratio = sm.ratio()
    print(f"token similarity after normalisation: {ratio * 100:.1f}%")
    print()

    shown = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal" or shown >= 12:
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            if shown >= 12:
                break
            raw_a = wa[i1 + k][4] if i1 + k < i2 else None
            raw_b = wb[j1 + k][4] if j1 + k < j2 else None
            print(f"  [{tag}]")
            if raw_a is not None:
                print(f"    A raw  {raw_a!r}")
                print(f"      code {codepoints(raw_a)}")
                print(f"      norm {norm.token(raw_a)!r}")
            if raw_b is not None:
                print(f"    B raw  {raw_b!r}")
                print(f"      code {codepoints(raw_b)}")
                print(f"      norm {norm.token(raw_b)!r}")
            if raw_a and raw_b and norm.token(raw_a) != norm.token(raw_b):
                print(f"      A chars: {describe(raw_a)}")
                print(f"      B chars: {describe(raw_b)}")
            shown += 1
    if shown == 0:
        print("  no text differences on this page")

    print()
    print("images")
    for tag, doc in (("A", da), ("B", db)):
        page = doc[pno]
        for info in page.get_images(full=True):
            xref = info[0]
            exact, ph, mean = image_fingerprint(
                doc, f"{tag}{id(doc)}", xref, True)
            rects = page.get_image_rects(xref)
            box = rects[0] if rects else None
            where = f"({box.x0:.0f},{box.y0:.0f})" if box else "?"
            phs = "none" if ph is None else f"{ph:#018x}"
            print(f"  {tag} xref={xref:<5} bytes={exact[:8].hex()} "
                  f"phash={phs} mean={mean} at {where}")
    da.close()
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
