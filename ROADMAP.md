# Roadmap

Features worth adding next, ordered by value-per-effort. Each note says where
it plugs into the existing architecture, because most of them are small once
the engine already streams through SQLite.

---

## Tier 1 — high value, days not weeks

### Batch / folder comparison
Compare `old/*.pdf` against `new/*.pdf` by filename, produce one roll-up report
plus a per-pair report. This is the single most requested feature in document
review teams and the engine already supports it — `ComparisonEngine` is
UI-agnostic and each pair gets its own session directory.

*Where:* a new `pdfdiff/batch.py` driving the engine in a loop, plus a queue
screen. Reuse `reporting.generate` per pair and add a `generate_index()`.

### Command-line interface
```
pdfdiff compare a.pdf b.pdf --report out/ --format html,csv --fail-on-change
```
Non-zero exit when differences are found makes it usable as a CI gate for
generated-document pipelines. No GUI import, so it runs headless.

*Where:* `pdfdiff/cli.py` with `argparse`. The engine, store and reporting
modules are already Qt-free.

### Table-aware comparison
Detect table structure with `page.find_tables()` (PyMuPDF ships this) and diff
cell-by-cell instead of as a word stream. A shifted column currently reads as a
large text change; cell-level diff would report "row 14, column 3: 1,200 →
1,350", which is what a finance reviewer actually wants.

*Where:* a new extractor pass in `extract.py` storing a cell grid, plus a
`diff_tables()` alongside `diff_words()`.

### Redline / annotated PDF export
Write a copy of document B with PDF annotations (highlights + popup notes) at
every change. Reviewers can then open it in Acrobat and comment inline. Roughly
40 lines using `page.add_highlight_annot()` — the highlight geometry is already
stored per page.

*Where:* `reporting.py`, next to `generate_pdf`.

### Saveable comparison profiles
Named bundles of settings + ignore rules ("Monthly statements", "Contract
redline"). Teams re-compare the same document families constantly and re-drawing
ignore regions every time is the main friction point.

*Where:* `CompareSettings` already serialises to JSON and hashes itself; this is
a profile picker plus a folder of JSON files.

---

## Tier 2 — differentiating

### OCR fallback for scanned pages
If a page yields zero words but has a full-page image, run OCR
(`page.get_textpage_ocr()`, needs Tesseract) and compare the recognised text.
Flag OCR pages in the UI as lower confidence, because they are.

*Where:* `extract.ingest_chunk`, behind a setting. Cache aggressively — OCR is
orders of magnitude slower than text extraction, so this needs its own progress
phase.

### Vector / line-art comparison
`page.get_drawings()` returns path geometry. Hash each path's normalised points
and diff the sets. Engineering drawings and charts currently compare as
"no text changed" even when the lines moved.

*Where:* a third payload list next to words and images.

### Semantic diff
Beyond "these words changed": classify a change as reworded / reordered /
substantive using sentence embeddings, and let the reviewer filter to
"substantive only". This is where an LLM or a local embedding model earns its
place — the diff engine feeds it candidate pairs.

*Where:* a post-pass over the stored `boxes`, writing a `severity` column to
the `pair` table. Keep it optional and offline-capable.

### Change severity scoring
Even without ML: weight changes by what they touch. A change inside a number, a
date, a currency amount or a defined term matters more than one in body prose.
Sort the page list by severity rather than raw count.

### Three-way comparison
Base / yours / theirs, for merge review. The alignment code already handles the
general case; the extension is aligning three fingerprint sequences and
rendering three panes.

---

## Tier 3 — polish and operations

- **Bookmark / comment changes** — diff the outline tree and annotation set,
  not just page content.
- **Metadata diff** — producer, fonts embedded, page sizes, encryption. Cheap
  and often the actual answer to "why do these look different?".
- **Per-page approve / reject workflow** — a reviewer state column in the store,
  exported in the report.
- **Watch folder** — compare automatically when a new version lands.
- **Digital signature check** — surface whether either document is signed and
  whether the signature still validates.
- **Accessibility pass** — keyboard-only navigation is mostly there via the
  shortcuts; add focus rings and screen-reader labels.
- **Server-side licence activation** — the honest fix for the client-side
  limitation documented in the README.

---

## Known limits of the current build

Worth fixing before they become support tickets.

| Limit | Why it exists | Fix |
|---|---|---|
| Rotated pages use unrotated coordinates for highlights | Word boxes come from PyMuPDF in unrotated space; the viewer renders rotated | Apply the page rotation matrix to highlight geometry in `PageScene` |
| Right-to-left and vertical scripts | Reading order is derived from block/line/word order, which assumes LTR | Detect direction per block and reverse token order |
| Very wide replace blocks pair positionally | `diff_words` pairs the first *n* words of each side | Run a second-level `SequenceMatcher` on characters within the block |
| HTML report holds all changed rows in memory | `generate_html` materialises the row list | Stream to the file; the store already yields lazily |
| One comparison at a time | `MainWindow` owns a single engine | Tab or queue model — falls out of batch comparison |
