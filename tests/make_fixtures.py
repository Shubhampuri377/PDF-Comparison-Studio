"""Generate synthetic PDF pairs for correctness and scale testing."""
from __future__ import annotations

import os
import random

try:
    import pymupdf as fitz
except ImportError:
    import fitz

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")

LOREM = ("consectetur adipiscing elit sed do eiusmod tempor incididunt ut "
         "labore et dolore magna aliqua enim ad minim veniam quis nostrud "
         "exercitation ullamco laboris nisi aliquip ex ea commodo consequat "
         "duis aute irure in reprehenderit voluptate velit esse cillum "
         "fugiat nulla pariatur excepteur sint occaecat cupidatat non "
         "proident sunt culpa qui officia deserunt mollit anim id est").split()


def _body(rng: random.Random, n_lines: int = 34) -> list[str]:
    return [" ".join(rng.choice(LOREM) for _ in range(11))
            for _ in range(n_lines)]


def build(path: str, n_pages: int, seed: int = 7, *,
          edit_pages: dict[int, str] | None = None,
          insert_after: set[int] | None = None,
          delete_pages: set[int] | None = None,
          shift_margin: float = 0.0,
          with_logo: bool = True,
          stamp_dates: bool = True):
    """Write a deterministic multi-page PDF.

    ``edit_pages`` maps a *source* page index to replacement text for its
    first body line; ``insert_after`` adds a brand new page; ``delete_pages``
    omits one entirely.  Together these exercise the alignment path.
    """
    edit_pages = edit_pages or {}
    insert_after = insert_after or set()
    delete_pages = delete_pages or set()

    doc = fitz.open()
    logo = None
    if with_logo:
        pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 48, 48))
        pix.set_rect(pix.irect, (30, 90, 170))
        logo = pix.tobytes("png")

    for src in range(n_pages):
        if src in delete_pages:
            continue
        rng = random.Random(seed * 1000 + src)
        page = doc.new_page(width=595, height=842)
        y = 70 + shift_margin

        # shift_margin nudges the *entire* page block, the way a real margin
        # or template change does -- header, body and footer together.
        page.insert_text((60, 44 + shift_margin),
                         f"ACME Quarterly Report — Section {src // 20 + 1}",
                         fontsize=13, fontname="hebo")
        if logo is not None:
            page.insert_image(fitz.Rect(500, 26 + shift_margin,
                                        548, 74 + shift_margin), stream=logo)

        lines = _body(rng)
        if src in edit_pages:
            lines[3] = edit_pages[src]
        for line in lines:
            page.insert_text((60, y), line, fontsize=9.5)
            y += 20.5

        if stamp_dates:
            page.insert_text((60, 812 + shift_margin),
                             f"Generated 2026-0{src % 9 + 1}-1{src % 9} 09:{src % 60:02d}",
                             fontsize=7.5)
        page.insert_text((520, 812 + shift_margin),
                         f"Page {doc.page_count}", fontsize=7.5)

        if src in insert_after:
            extra = doc.new_page(width=595, height=842)
            extra.insert_text((60, 70), "INSERTED ADDENDUM PAGE",
                              fontsize=15, fontname="hebo")
            for k in range(12):
                extra.insert_text((60, 110 + k * 20),
                                  f"Addendum clause {k + 1} appended during review.",
                                  fontsize=9.5)

    doc.save(path, deflate=True, garbage=3)
    doc.close()


def make_small_pair() -> tuple[str, str]:
    os.makedirs(FIXTURES, exist_ok=True)
    a = os.path.join(FIXTURES, "small_a.pdf")
    b = os.path.join(FIXTURES, "small_b.pdf")
    build(a, 40)
    build(b, 40,
          edit_pages={5: "THIS LINE WAS COMPLETELY REPLACED IN VERSION B",
                      17: "another modified sentence appears right here now"},
          insert_after={9},
          delete_pages={30})
    return a, b


def make_margin_pair() -> tuple[str, str]:
    """Same text, whole body nudged down -- must NOT read as 'all moved'."""
    os.makedirs(FIXTURES, exist_ok=True)
    a = os.path.join(FIXTURES, "margin_a.pdf")
    b = os.path.join(FIXTURES, "margin_b.pdf")
    build(a, 6, shift_margin=0.0)
    build(b, 6, shift_margin=9.0)
    return a, b


def make_identical_pair() -> tuple[str, str]:
    os.makedirs(FIXTURES, exist_ok=True)
    a = os.path.join(FIXTURES, "ident_a.pdf")
    b = os.path.join(FIXTURES, "ident_b.pdf")
    build(a, 25)
    build(b, 25)
    return a, b


def build_large(path: str, n_pages: int, *, seed: int = 7,
                block: int = 1000,
                edit_every: int = 0,
                insert_at: int | None = None,
                delete_at: int | None = None):
    """Build a large fixture without paying full price for every page.

    Laying out 34 fresh text lines on 20,000 pages means 680,000
    ``insert_text`` calls and roughly nine minutes per document, which makes
    the benchmark unusable.  Instead a unique ``block`` of pages is composed
    once and stitched repeatedly, then every page is stamped with its own
    reference string.

    That leaves each page's fingerprint unique — so alignment still has real
    anchors to work with — while the body text repeats the way boilerplate
    genuinely does in large filings.
    """
    rng_master = random.Random(seed)

    template = fitz.open()
    for i in range(min(block, n_pages)):
        rng = random.Random(seed * 1000 + i)
        page = template.new_page(width=595, height=842)
        page.insert_text((60, 44), f"ACME Filing — Section {i // 25 + 1}",
                         fontsize=13, fontname="hebo")
        y = 70
        for line in _body(rng):
            page.insert_text((60, y), line, fontsize=9.5)
            y += 20.5

    doc = fitz.open()
    while doc.page_count < n_pages:
        remaining = n_pages - doc.page_count
        doc.insert_pdf(template, from_page=0,
                       to_page=min(template.page_count, remaining) - 1)
    template.close()

    # Track which *logical* page each output slot holds, so the reference
    # stamp stays with the content rather than with the position.  Stamping by
    # output index instead would give every page after an insertion a brand
    # new identity, leaving page alignment with no anchors to work from -- the
    # fixture would then measure the fallback path rather than the real one.
    ordinals: list[int | None] = list(range(n_pages))

    if delete_at is not None and 0 <= delete_at < doc.page_count:
        doc.delete_page(delete_at)
        ordinals.pop(delete_at)

    if insert_at is not None and 0 <= insert_at <= doc.page_count:
        extra = doc.new_page(pno=insert_at, width=595, height=842)
        extra.insert_text((60, 70), "INSERTED ADDENDUM PAGE",
                          fontsize=15, fontname="hebo")
        for k in range(12):
            extra.insert_text((60, 110 + k * 20),
                              f"Addendum clause {k + 1} appended during review.",
                              fontsize=9.5)
        ordinals.insert(insert_at, None)     # None marks the new page

    # One insert_text per page rather than 34.
    for pno in range(doc.page_count):
        src = ordinals[pno] if pno < len(ordinals) else None
        page = doc.load_page(pno)
        if src is not None:
            token = random.Random(seed * 7919 + src).randrange(1 << 30)
            page.insert_text((60, 806), f"REF-{src:07d}-{token:09d}",
                             fontsize=7.5)
            if edit_every and src % edit_every == 0:
                page.insert_text((60, 132),
                                 "SUBSTANTIVE EDIT APPLIED DURING LEGAL REVIEW",
                                 fontsize=9.5, color=(0.7, 0, 0))
        # The visible page number legitimately renumbers around an insertion.
        # Keeping it is the point: it is the noise that ignore rules remove.
        page.insert_text((470, 806), f"Page {pno + 1} of {doc.page_count}",
                         fontsize=7.5)

    # No garbage collection pass: it is superlinear and adds minutes here.
    # Written to a temporary name and renamed, so an interrupted 18-minute
    # build leaves no zero-byte file that the next run mistakes for a
    # finished fixture and then fails to open.
    tmp = path + ".partial"
    doc.save(tmp, deflate=True, garbage=0)
    doc.close()
    os.replace(tmp, path)


def _usable(path: str) -> bool:
    """A fixture counts as present only if it is non-empty."""
    try:
        return os.path.getsize(path) > 1024
    except OSError:
        return False


def make_large_pair(n_pages: int = 20000) -> tuple[str, str]:
    os.makedirs(FIXTURES, exist_ok=True)
    a = os.path.join(FIXTURES, f"large_{n_pages}_a.pdf")
    b = os.path.join(FIXTURES, f"large_{n_pages}_b.pdf")
    if not _usable(a):
        build_large(a, n_pages)
    if not _usable(b):
        # Same base content, plus: an edit every 250 pages, one inserted page
        # near the front and one deletion in the middle.  The insert near the
        # front is the important part -- without page alignment it cascades
        # into every later page reading as rewritten.
        build_large(b, n_pages, edit_every=250, insert_at=17,
                    delete_at=n_pages // 2)
    return a, b


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    if n:
        pa, pb = make_large_pair(n)
    else:
        pa, pb = make_small_pair()
    for p in (pa, pb):
        print(f"{p}  {os.path.getsize(p) / 1e6:.1f} MB")
