"""Explain why a page was reported as added or removed.

    python tools/explain_page.py A.pdf B.pdf --page 991

Answers the question "that page IS there, why is the tool saying otherwise?"
with evidence rather than an assertion: it takes the tokens that identify the
page -- the ones that are rare across the document, not the letterhead every
page shares -- and reports whether they exist anywhere in the other file.
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pymupdf as fitz
except ImportError:
    import fitz

TOKEN_RE = re.compile(r"[A-Za-z0-9][\w\-/\.]{2,}")
RARE_DF = 5


def page_tokens(doc, pno: int) -> set[str]:
    return set(TOKEN_RE.findall(doc.load_page(pno).get_text()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("doc_a")
    ap.add_argument("doc_b")
    ap.add_argument("--page", type=int, required=True,
                    help="1-based page number to explain")
    ap.add_argument("--side", choices=("a", "b"), default="a",
                    help="which document the page belongs to")
    ap.add_argument("--show", type=int, default=12)
    args = ap.parse_args()

    src_path = args.doc_a if args.side == "a" else args.doc_b
    other_path = args.doc_b if args.side == "a" else args.doc_a
    src, other = fitz.open(src_path), fitz.open(other_path)

    pno = args.page - 1
    if not 0 <= pno < src.page_count:
        print(f"page {args.page} is outside {os.path.basename(src_path)} "
              f"({src.page_count} pages)")
        return 1

    print(f"Explaining page {args.page} of {os.path.basename(src_path)}")
    print(f"against {os.path.basename(other_path)} ({other.page_count} pages)")
    print()

    print("indexing the other document ...")
    other_bags = [page_tokens(other, j) for j in range(other.page_count)]
    df: collections.Counter = collections.Counter()
    for bag in other_bags:
        df.update(bag)

    where = collections.defaultdict(list)
    for j, bag in enumerate(other_bags):
        for t in bag:
            if df[t] <= RARE_DF:
                where[t].append(j)

    mine = page_tokens(src, pno)
    identifying = sorted(t for t in mine if df.get(t, 0) <= RARE_DF)
    boiler = len(mine) - len(identifying)

    print()
    print(f"  tokens on this page        : {len(mine)}")
    print(f"  shared with most pages     : {boiler}  (letterhead, legal text)")
    print(f"  identifying tokens         : {len(identifying)}")
    print()

    if not identifying:
        print("  This page has nothing that distinguishes it from its")
        print("  neighbours, so it can only be matched by position.")
        return 0

    found, missing = [], []
    votes: collections.Counter = collections.Counter()
    for t in identifying:
        pages = where.get(t)
        if pages:
            found.append((t, pages))
            for j in pages:
                votes[j] += 1
        else:
            missing.append(t)

    print(f"  identifying tokens found in the other document : "
          f"{len(found)} / {len(identifying)}")
    print()
    for t, pages in found[:args.show]:
        shown = ", ".join(str(j + 1) for j in pages[:5])
        print(f"    FOUND    {t:<24} on page(s) {shown}")
    for t in missing[:args.show]:
        print(f"    MISSING  {t:<24} appears nowhere")

    print()
    if votes:
        best, n = votes.most_common(1)[0]
        print(f"  best candidate partner: page {best + 1} "
              f"sharing {n} of {len(identifying)} identifying tokens")
    ratio = len(found) / len(identifying)
    print()
    if ratio >= 0.6:
        print("  VERDICT: this page most likely does exist in the other")
        print("           document. If the tool reported it removed, the")
        print("           alignment placed it wrongly -- please report it.")
    elif ratio <= 0.2:
        print("  VERDICT: the content that identifies this page is genuinely")
        print("           absent from the other document. Reporting it as")
        print("           removed is correct.")
    else:
        print("  VERDICT: partially present. The page was probably edited")
        print("           heavily rather than moved or removed outright.")

    src.close()
    other.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
