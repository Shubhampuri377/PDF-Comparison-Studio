# PDF Comparison Studio

Desktop PDF diff tool. Word-level text differences, embedded-image changes,
formatting changes, and automatic detection of inserted/deleted pages —
designed to stay responsive on documents in the tens of thousands of pages.

```bash
python app.py
```

---

## What it does

| | |
|---|---|
| **Word-level diff** | `difflib` over normalised token streams, with the surviving word boxes drawn as vector overlays on the rendered page |
| **Page alignment** | Insert one page at the front of a 20,000-page document and only that page is reported — not 19,999 false positives |
| **Ignore areas** | Drag a rectangle over the page; scope it to all/odd/even/specific pages and to either document |
| **Ignore patterns** | Regex exclusions with presets for page numbers, dates, times, Bates stamps, UUIDs, currency |
| **Image comparison** | Exact-byte matching first, then perceptual hashing so a re-encoded logo reads as unchanged, not as removed-and-added |
| **Formatting diff** | Optional font / size / colour / bold-italic change detection |
| **Reports** | Self-contained HTML, printable PDF, CSV and JSON |
| **Resumable sessions** | Results live in SQLite; changing ignore rules re-diffs without re-reading a single PDF |

---

## Architecture

```
 app.py  ──►  pdfdiff.ui.main          Qt shell, screen switching, workers
                     │
                     ▼
              pdfdiff.engine           orchestrator: probe → ingest → align → diff
                     │
     ┌───────────────┼───────────────┬──────────────────┐
     ▼               ▼               ▼                  ▼
 extract.py      align.py       diffcore.py         store.py
 (workers)     (patience LCS)   (workers)          (SQLite)
```

Everything under `pdfdiff/` **except** `pdfdiff/ui/` is import-safe inside a
multiprocessing worker — no Qt, no global app state. Do not break that: on
Windows each worker re-imports these modules from scratch.

### The four phases

1. **probe** — metadata, page counts, a session directory keyed on a content
   fingerprint of both files.
2. **ingest** — every page of both documents parsed in parallel across a
   process pool. Word boxes, image references and two page fingerprints are
   compressed and written to SQLite. This is the only phase that touches the
   PDFs.
3. **align** — page fingerprints matched with patience anchoring
   (O(n log n)), gaps resolved by Needleman–Wunsch on simhash similarity.
4. **diff** — aligned pairs compared in parallel. Workers read page payloads
   straight from SQLite rather than receiving them over a pipe.

Because only `ingest` reads the PDFs, editing an ignore region re-runs
`align`/`diff` alone. That is what makes the ignore editor usable at scale.

### Measured at 20,000 pages

Two 20,000-page documents (180 MB each), 7 worker processes, on a Windows
laptop. This fixture is a deliberate worst case: half the pages are completely
rewritten, producing 6.9 million individual changes. Real comparisons are far
lighter.

| | |
|---|---|
| Full run — parse, align, diff 40,000 pages | **224 s** |
| Same run with page data already cached | **117 s** |
| Re-diff after changing ignore rules | **44 s** (no PDF re-parse) |
| Page alignment, 20,000 vs 20,000 fingerprints | **0.04 s** |
| Peak RSS — parent process | **369 MB** |
| Peak RSS — all 8 processes | 2,630 MB |
| Session on disk | 452 MB |

The parent staying flat at 369 MB is the result that matters: nothing
accumulates per page. The 2.6 GB total is seven worker processes at roughly
330 MB each, which is almost entirely the per-process Python + PyMuPDF
baseline rather than comparison data. Lowering the worker count in
**Settings → Performance** trades wall time for memory close to linearly.

Reproduce with `python -m tests.benchmark 20000`. The first run spends about
18 minutes per document generating fixtures.

### Why it holds up at 20,000 pages

- Page payloads are compressed to disk during ingest; nothing accumulates a
  per-page list in RAM.
- The page navigator is a `QAbstractListModel` over a list of integers, with
  statistics fetched in windows and thumbnails rendered only for visible rows.
- Zoom is a `QGraphicsView` transform. Highlights are vector items in page-point
  coordinates, so zooming never re-runs a diff or re-encodes a bitmap.
- The render cache is byte-bounded (default 384 MB) rather than item-bounded.
- Rasterisation is capped at 40 megapixels per page, so a deep zoom on a large
  format drawing cannot ask for a multi-gigabyte bitmap.

---

## Comparison settings

Reachable from **Settings** on either screen.

**Text** — ignore case / punctuation / digits, whitespace normalisation,
movement tolerance, formatting detection.

**Layout & images** — header and footer bands in points (72 pt = 1 inch),
image comparison and its hash/movement tolerances, page-alignment toggle and
similarity threshold.

**Ignore patterns** — one regex per line. A malformed pattern is skipped, not
fatal. Presets cover the usual suspects.

**Performance** — worker count, pages per work unit, image cache size, and the
per-page highlight cap. Counts always stay exact; only drawn rectangles are
capped.

### Two behaviours worth knowing

**Uniform shifts are not "moved text."** The dominant (dx, dy) across matched
words is subtracted before movement is measured, so changing a top margin
reports a single `Content shifted as a block` note rather than a move marker on
every word.

**Reflowed pages are flagged, not enumerated.** If more than 60% of a page's
words appear moved, the page was reflowed; the category is suppressed and the
page is flagged instead.

---

## Licensing

Ed25519. The application holds only the **public** key
(`pdfdiff/licensing.py: PUBLIC_KEY_B64`). The private seed stays on the
issuing machine.

```bash
# once, on the issuing machine
python tools/keygen.py --write-dev   # writes tools/signing_seed.txt (gitignored)
# paste the printed public key into pdfdiff/licensing.py PUBLIC_KEY_B64

# per customer
python tools/issue_license.py "Acme Legal" --days 365
python tools/issue_license.py "Acme Legal" --days 30 --machine <their-machine-id>

# check your seed and the app's key still agree
python tools/install_dev_license.py --diagnose
```

The customer's Machine ID is shown in the app's Licence dialog, with a Copy
button. Omit `--machine` for a licence that runs anywhere.

**Offline licences cannot be revoked.** A key works until it expires, so issue
30 or 90 days and reissue on renewal rather than handing out long ones.

`issue_license.py` warns if the seed it used does not match the public key
compiled into the app, so a mismatch is caught before a customer sees it.

**What this does and does not buy you.** Forging a licence now requires the
private seed, which is not in the binary. It does not stop someone patching the
check out of a cracked executable — no client-side scheme can. If that matters,
move to server-side activation.

Verification runs in ~10 ms pure-Python; install `cryptography` for a faster
path. The implementation is checked against the RFC 8032 test vectors.

Also handled: expiry is compared in UTC (a timezone change no longer extends a
licence), failures report the actual reason, and the re-validation watchdog
actually runs on a 60-second interval.

Clock-rollback detection writes a last-seen timestamp to both a file and
`HKCU`, taking the newer. It is tamper-*evident*, not tamper-proof — it stops
the casual "set the clock back" bypass and nothing stronger.

---

## Reports

**HTML** — self-contained, light/dark aware, sortable and filterable summary
table plus embedded page images. Nothing loads from the network.

**PDF** — A4 landscape, cover page with statistics, then annotated side-by-side
spreads.

**CSV / JSON** — every page with per-category counts, for pipelines and
spreadsheets.

Page rendering is capped by the **Maximum pages to render** setting. Pages past
the cap still appear in the summary table and in the CSV/JSON exports, and the
report says so explicitly rather than silently truncating.

---

## Development

```bash
python -m tests.test_core          # correctness suite
python -m tests.benchmark 20000    # scale benchmark
python -m tests.make_fixtures      # regenerate small fixtures
```

### Running and building

```bat
run.bat                 launch the app
build.bat               build dist\PDFComparisonStudio.exe
```

`run.bat` uses `.venv` if present and launches without a console window.

`build.bat` runs pre-flight checks, builds, then executes the packaged binary's
own self test before declaring success:

```bat
dist\PDFComparisonStudio.exe --selftest
```

That step matters. A PyInstaller build can link cleanly and still die the first
time it spawns a worker -- a missing hidden import, or a `freeze_support`
mistake that relaunches the whole GUI per worker. The self test builds two
40-page documents, compares them across a real worker pool, and reports what it
found, so a broken build is caught before it ships.

Pre-flight (`tools/preflight.py`) refuses to build when:

- a runtime dependency is missing
- the app's `PUBLIC_KEY_B64` does not match your signing seed -- otherwise you
  ship a binary you can never issue a licence for
- `app.spec` references a seed file, which would bundle your private key
- any module imports a package the spec excludes
- `app.py` has lost its `__main__` guard or `freeze_support()` call

Poppler is gone — PyMuPDF rasterises pages now, removing ~100 MB of vendored
binaries and the subprocess-per-page cost. `console=False`, so no stray console
window behind the GUI.

### One trap to know about

On Windows, `multiprocessing` uses **spawn**: each worker re-imports the
`__main__` module. Any script that drives `ComparisonEngine` must guard its
entry point, or every worker re-runs the whole script:

```python
if __name__ == "__main__":
    main()
```

`app.py` does this and calls `freeze_support()`. Your own scripts must too.

---

## What replaced v1

The original CustomTkinter build (`main.py`, `pdf_engine.py`,
`license_manager.py`, `main.spec`, `tools/generate_license.py`) and its
vendored `poppler-25.12.0/` have been removed. They remain in git history and
can be restored at any time:

```bash
git checkout HEAD -- PDF_Comparison_Tool/
```

Behavioural differences between the two:

| v1 | v2 |
|---|---|
| Compared page *i* to page *i* | Aligns pages; reports insertions and deletions |
| "Only Pages with Issues" compared raw tuples including float coordinates, so identical text often flagged | Filter uses the real diff result |
| Replace blocks zipped both sides, silently dropping the tail when lengths differed | Full tail reported as added/removed |
| Whole document held in RAM | Streams through SQLite |
| Poppler subprocess per page render | PyMuPDF in-process |
| Progress bar written from a worker thread | Qt signals |
| Unbounded zoom re-encoding a full-page bitmap per step | View transform, capped rasterisation |
