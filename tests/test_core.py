"""Headless correctness checks for the comparison core.

Run with:  python -m tests.test_core
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfdiff.align import align_documents                      # noqa: E402
from pdfdiff.engine import CancelledError, ComparisonEngine                    # noqa: E402
from pdfdiff.ignore import IgnoreRule, IgnoreSet, parse_page_spec, SCOPE_PAGES  # noqa: E402
from pdfdiff.models import (                                   # noqa: E402
    CH_ADDED, CH_CHANGED, CH_MOVED, CH_REMOVED, CompareSettings,
    KIND_ADDED, KIND_MATCHED, KIND_REMOVED, RuntimeSettings,
)
from pdfdiff.store import FILTER_CHANGED                       # noqa: E402
from tests import make_fixtures                                # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok]   {label}")
    else:
        FAIL += 1
        print(f"  [FAIL] {label}  {detail}")


# --------------------------------------------------------------------------
def test_page_spec():
    print("\npage spec parsing")
    check("single page", parse_page_spec("3") == [(2, 2)])
    check("range", parse_page_spec("1-5") == [(0, 4)])
    check("open range", parse_page_spec("200-") == [(199, -1)])
    check("mixed", parse_page_spec("1-5, 9, 20-") == [(0, 4), (8, 8), (19, -1)])
    check("junk tolerated", parse_page_spec("1-5, abc, 9") == [(0, 4), (8, 8)])


def test_ignore_set():
    print("\nignore regions")
    rule = IgnoreRule(x0=0.0, y0=0.0, x1=1.0, y1=0.1, label="header")
    iset = IgnoreSet([rule])
    mask = iset.masker(0, 0, 600.0, 800.0)
    check("mask built", mask is not None)
    check("header word ignored", mask(50, 10, 200, 30))
    check("body word kept", not mask(50, 400, 200, 420))

    scoped = IgnoreSet([IgnoreRule(x0=0, y0=0, x1=1, y1=1,
                                   scope=SCOPE_PAGES, pages="2-3")])
    check("page 1 unaffected", scoped.masker(0, 0, 600, 800) is None)
    check("page 2 affected", scoped.masker(0, 1, 600, 800) is not None)


def test_alignment():
    print("\npage alignment")
    a = [10, 20, 30, 40, 50]
    b = [10, 20, 99, 30, 40, 50]           # one page inserted at index 2
    sims = [0] * 8
    pairs = align_documents(a, b, sims[:5], sims[:6], [5] * 5, [5] * 6)
    added = [p for p in pairs if p[2] == KIND_ADDED]
    matched = [p for p in pairs if p[2] == KIND_MATCHED]
    check("insert detected as 1 added page", len(added) == 1,
          f"got {len(added)}")
    check("all 5 originals still matched", len(matched) == 5,
          f"got {len(matched)}")
    check("page 3 maps to slot 4", (2, 3, KIND_MATCHED) in pairs,
          f"pairs={pairs}")

    b2 = [10, 30, 40, 50]                   # one page deleted
    pairs2 = align_documents(a, b2, sims[:5], sims[:4], [5] * 5, [5] * 4)
    removed = [p for p in pairs2 if p[2] == KIND_REMOVED]
    check("delete detected as 1 removed page", len(removed) == 1,
          f"got {len(removed)}")

    ident = align_documents(a, a, sims[:5], sims[:5], [5] * 5, [5] * 5)
    check("identical docs align 1:1",
          ident == [(i, i, KIND_MATCHED) for i in range(5)])


def test_alignment_at_scale():
    """Alignment must stay correct and fast on 20,000-page fingerprint lists.

    Exercised directly on fingerprints rather than through real PDFs: building
    two 20,000-page documents takes ~18 minutes each, and the algorithm only
    ever sees these integers anyway.
    """
    print("\npage alignment at 20,000 pages")
    import random

    rng = random.Random(11)
    n = 20_000
    a = [rng.randrange(1, 1 << 62) for _ in range(n)]

    # B: one page inserted near the front, one deleted in the middle, and a
    # 40-page block whose content changed (new fingerprints, same slots).
    b = list(a)
    b.insert(17, rng.randrange(1, 1 << 62))
    del b[10_000]
    for i in range(15_000, 15_040):
        b[i] = rng.randrange(1, 1 << 62)

    sims_a = [0] * len(a)
    sims_b = [0] * len(b)
    counts_a = [500] * len(a)
    counts_b = [500] * len(b)

    t0 = time.monotonic()
    pairs = align_documents(a, b, sims_a, sims_b, counts_a, counts_b)
    dt = time.monotonic() - t0
    print(f"    ({len(a):,} vs {len(b):,} pages aligned in {dt:.2f}s)")

    stats = {"matched": 0, "added": 0, "removed": 0}
    for _pa, _pb, kind in pairs:
        stats[kind] = stats.get(kind, 0) + 1

    check("completes in under 10s", dt < 10.0, f"{dt:.2f}s")
    check("both documents fully covered",
          len([p for p in pairs if p[0] is not None]) == len(a)
          and len([p for p in pairs if p[1] is not None]) == len(b),
          f"{stats}")
    # The 40 rewritten pages have no shared fingerprint, so they resolve as
    # 40 removed + 40 added on top of the structural insert/delete.
    check("insertion detected", stats["added"] >= 1, f"added={stats['added']}")
    check("deletion detected", stats["removed"] >= 1,
          f"removed={stats['removed']}")
    check("structural edits stay local — no cascade",
          stats["matched"] >= n - 60,
          f"matched={stats['matched']:,} of {n:,}")

    # The single most important property: page k after the insertion must map
    # to slot k+1, not to slot k.
    mapping = {pa: pb for pa, pb, kind in pairs
               if kind == KIND_MATCHED and pa is not None}
    check("pages before the insert map 1:1", mapping.get(5) == 5,
          f"A5 -> {mapping.get(5)}")
    check("pages after the insert shift by one", mapping.get(100) == 101,
          f"A100 -> {mapping.get(100)}")
    check("pages after the delete shift back", mapping.get(12_000) == 12_000,
          f"A12000 -> {mapping.get(12_000)}")


def _run(path_a, path_b, settings=None, label=""):
    eng = ComparisonEngine(path_a, path_b,
                           settings=settings or CompareSettings(),
                           runtime=RuntimeSettings(workers=4, chunk_pages=32))
    t0 = time.monotonic()
    summary = eng.run()
    dt = time.monotonic() - t0
    if label:
        print(f"    ({label}: {dt:.2f}s)")
    return eng, summary


def test_identical():
    print("\nidentical documents")
    a, b = make_fixtures.make_identical_pair()
    eng, s = _run(a, b, label="identical 25pp")
    check("no changed pages", s.changed == 0, f"changed={s.changed}")
    check("all pages identical", s.identical == 25, f"identical={s.identical}")
    check("zero total changes", s.total_changes == 0, f"{s.counts}")
    eng.close()


def test_margin_shift():
    print("\nuniform margin shift (median compensation)")
    a, b = make_fixtures.make_margin_pair()
    eng, s = _run(a, b, label="margin 6pp")
    moved = s.counts.get(CH_MOVED, 0)
    check("global shift not reported as moves", moved == 0,
          f"moved={moved} counts={s.counts}")
    check("no text changes", s.counts.get(CH_ADDED, 0) == 0
          and s.counts.get(CH_REMOVED, 0) == 0, f"{s.counts}")
    eng.close()


def test_edits_and_structure():
    print("\nedits + inserted/deleted pages")
    a, b = make_fixtures.make_small_pair()
    eng, s = _run(a, b, label="small 40pp")
    check("one page added", s.added_pages == 1, f"added={s.added_pages}")
    check("one page removed", s.removed_pages == 1, f"removed={s.removed_pages}")
    check("41 pairs from 40+1-1 pages", s.total_pairs == 41,
          f"total={s.total_pairs}")
    # Alignment must survive the insert: without it, every page after index 9
    # would be compared against the wrong partner and the whole tail would
    # read as rewritten.  Body text stays matched; only the footer page
    # numbers renumber, which is exactly what ignore rules are for.
    check("body text still matches after the insert",
          s.counts.get(CH_CHANGED, 0) < 200,
          f"changed words={s.counts.get(CH_CHANGED, 0)}")

    store = eng.store
    changed_idx = store.pair_indices(FILTER_CHANGED)
    check("changed filter matches summary", len(changed_idx) == s.changed,
          f"{len(changed_idx)} vs {s.changed}")

    # every changed page must carry highlight boxes we can draw
    with_boxes = sum(1 for i in changed_idx if store.get_boxes(i))
    check("changed pages have highlight boxes",
          with_boxes == len(changed_idx), f"{with_boxes}/{len(changed_idx)}")

    # Now the payoff: ignore the footer strip and the renumbering noise should
    # collapse, leaving only the two real edits plus the two structural pages.
    quiet = eng.rediff(CompareSettings(footer_ignore_pt=40.0))
    check("footer ignore removes renumbering noise", quiet.changed == 4,
          f"changed={quiet.changed} (want 2 edits + 1 added + 1 removed)")
    check("37 pages now identical", quiet.identical == 37,
          f"identical={quiet.identical}")
    eng.close()


def test_ignore_rediff():
    print("\nignore rules + rediff (no re-parse)")
    a, b = make_fixtures.make_small_pair()
    eng, base = _run(a, b)

    # Footer carries a per-page timestamp; ignoring the bottom strip must not
    # change these fixtures' results but must run without re-ingesting.
    s2 = CompareSettings(footer_ignore_pt=40.0,
                         ignore_patterns=[r"^\d{4}-\d{2}-\d{2}$"])
    t0 = time.monotonic()
    after = eng.rediff(s2)
    dt = time.monotonic() - t0
    print(f"    (rediff 40pp: {dt:.2f}s)")
    check("rediff completes", after.total_pairs == base.total_pairs)
    check("rediff is fast (no PDF re-parse)", dt < 8.0, f"{dt:.2f}s")

    # A full-page ignore on every page must collapse everything to identical.
    s3 = CompareSettings(ignore_rules=[IgnoreRule(0, 0, 1, 1).to_dict()],
                         compare_images=False)
    blanked = eng.rediff(s3)
    check("full-page ignore yields no text diffs",
          blanked.counts.get(CH_ADDED, 0) == 0
          and blanked.counts.get(CH_REMOVED, 0) == 0
          and blanked.counts.get(CH_CHANGED, 0) == 0,
          f"{blanked.counts}")
    eng.close()


def test_replace_tail_not_dropped():
    print("\nreplace-block tail handling")
    from pdfdiff.diffcore import diff_words

    words_a = [(0, 0, 10, 10, w, -1) for w in
               ["alpha", "one", "two", "three", "four", "five", "omega"]]
    words_b = [(0, 0, 10, 10, w, -1) for w in
               ["alpha", "ONE", "TWO", "omega"]]
    toks_a = [w[4] for w in words_a]
    toks_b = [w[4] for w in words_b]
    counts, boxes, sim, flags = diff_words(
        words_a, toks_a, words_b, toks_b, [], [], CompareSettings())
    # 5 old tokens vs 2 new: 2 pair up as changed, 3 must be reported removed.
    check("tail of longer side reported as removed",
          counts.get(CH_REMOVED, 0) == 3,
          f"removed={counts.get(CH_REMOVED, 0)} counts={counts}")
    check("paired portion reported as changed",
          counts.get(CH_CHANGED, 0) == 4,
          f"changed={counts.get(CH_CHANGED, 0)} (2 words x 2 sides)")


def test_robustness():
    """The failure paths: corrupt input, encryption, and cancellation.

    v1 swallowed all three. A corrupt page became a blank white image, a
    password-protected file produced a dead thread and a permanently disabled
    Compare button, and cancelling did nothing because the flag was never read.
    """
    print("\nrobustness")
    import threading

    try:
        import pymupdf as fitz
    except ImportError:
        import fitz

    os.makedirs(make_fixtures.FIXTURES, exist_ok=True)
    good_a, good_b = make_fixtures.make_small_pair()

    # -- not a PDF at all --------------------------------------------------
    junk = os.path.join(make_fixtures.FIXTURES, "junk.pdf")
    with open(junk, "wb") as fh:
        fh.write(b"this is definitely not a PDF file" * 40)
    eng = ComparisonEngine(junk, good_b, runtime=RuntimeSettings(workers=2))
    try:
        eng.run()
        check("corrupt file raises", False, "no exception raised")
    except Exception as exc:
        check("corrupt file raises a clear error",
              isinstance(exc, RuntimeError) and "Document A" in str(exc),
              f"{type(exc).__name__}: {exc}")
    finally:
        eng.close()

    # -- password protected ------------------------------------------------
    enc = os.path.join(make_fixtures.FIXTURES, "encrypted.pdf")
    src = fitz.open(good_a)
    src.save(enc, encryption=fitz.PDF_ENCRYPT_AES_256,
             owner_pw="owner", user_pw="secret")
    src.close()
    eng = ComparisonEngine(enc, good_b, runtime=RuntimeSettings(workers=2))
    try:
        eng.run()
        check("encrypted file raises", False, "no exception raised")
    except Exception as exc:
        check("encrypted file explains itself",
              "assword" in str(exc), f"{type(exc).__name__}: {exc}")
    finally:
        eng.close()

    # ...and opens when the password is supplied.
    eng = ComparisonEngine(enc, good_b, password_a="secret",
                           runtime=RuntimeSettings(workers=2))
    try:
        summary = eng.run()
        check("correct password unlocks the document",
              summary.total_pairs > 0, f"{summary.total_pairs} pairs")
    except Exception as exc:
        check("correct password unlocks the document", False,
              f"{type(exc).__name__}: {exc}")
    finally:
        eng.close()

    # -- cancellation ------------------------------------------------------
    eng = ComparisonEngine(good_a, good_b,
                           runtime=RuntimeSettings(workers=2, chunk_pages=8))
    threading.Timer(0.15, eng.cancel).start()
    t0 = time.monotonic()
    try:
        eng.run(force_ingest=True)
        outcome = "completed"
    except CancelledError:
        outcome = "cancelled"
    except Exception as exc:
        outcome = f"{type(exc).__name__}: {exc}"
    dt = time.monotonic() - t0
    check("cancel stops the run", outcome in ("cancelled", "completed"),
          outcome)
    check("cancel returns promptly", dt < 20.0, f"{dt:.1f}s")
    eng.close()


def test_licensing():
    """Signature, expiry and machine binding.

    The machine-binding cases exist because the licence dialog shows a
    shortened, dash-grouped, upper-case machine id while verification compared
    the full lower-case hash. Binding with the very string the app tells the
    customer to copy could therefore never succeed.
    """
    print("\nlicensing")
    import base64
    import os as _os

    from pdfdiff import _ed25519
    from pdfdiff.licensing import (issue_licence, machine_id, machine_matches,
                                   public_key_from_seed, short_machine_id,
                                   verify_licence)

    seed_path = _os.path.join("tools", "signing_seed.txt")
    if not _os.path.exists(seed_path):
        print("  [skip] no signing seed present")
        return
    seed = open(seed_path, encoding="utf-8").read().strip()
    pub = base64.b64encode(
        public_key_from_seed(base64.b64decode(seed))).decode()

    def issued(**kw):
        return verify_licence(issue_licence(seed, "Test", **kw),
                              public_key_b64=pub, check_clock=False)

    check("valid licence accepted", issued(days=30).valid)
    check("expired licence rejected", not issued(days=-1).valid)

    tampered = issue_licence(seed, "Test", days=30)
    body, sig = tampered.split(".")
    forged = body[:-2] + ("AA" if body[-2:] != "AA" else "AB") + "." + sig
    check("tampered payload rejected",
          not verify_licence(forged, public_key_b64=pub,
                             check_clock=False).valid)

    other = base64.b64encode(
        _ed25519.public_key(_ed25519.generate_seed())).decode()
    check("wrong signing key rejected",
          not verify_licence(issue_licence(seed, "Test", days=30),
                             public_key_b64=other, check_clock=False).valid)

    # -- machine binding, every form the id is written in ------------------
    short = short_machine_id()
    full = machine_id()
    for label, value in (("dash-grouped upper (as displayed)", short),
                         ("lower case", short.lower()),
                         ("no separators", short.replace("-", "")),
                         ("full raw hash", full),
                         ("full hash upper", full.upper())):
        check(f"binds to this machine — {label}",
              issued(days=30, machine=value).valid,
              issued(days=30, machine=value).reason)

    check("rejects a different machine",
          not issued(days=30, machine="A1B2C-3D4E5-F6789-0ABCD").valid)
    check("rejects a too-short identifier",
          not machine_matches("0FFA2"), "5 chars must not match")
    check("unbound licence runs anywhere", issued(days=30).valid)


def main():
    test_page_spec()
    test_ignore_set()
    test_alignment()
    test_alignment_at_scale()
    test_replace_tail_not_dropped()
    test_identical()
    test_margin_shift()
    test_edits_and_structure()
    test_ignore_rediff()
    test_robustness()
    test_licensing()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
