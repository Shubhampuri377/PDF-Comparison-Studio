"""Page ingest: runs inside multiprocessing workers.

Import rules for this module are strict -- it is imported fresh in every spawned
worker on Windows, so it must not pull in Qt, must not touch global app state,
and must not be slow to import.

What ingest produces per page is deliberately small and self-describing:
word boxes, image references, and two fingerprints used later for page
alignment.  It does *not* diff anything; diffing needs both sides and the
user's settings, which can change without re-parsing a single PDF.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import unicodedata

try:                                    # PyMuPDF >= 1.24 prefers this name
    import pymupdf as fitz
except ImportError:                     # pragma: no cover - older installs
    import fitz

# --------------------------------------------------------------------------
# Per-process caches.  Workers are reused across chunks, so keeping the parsed
# document and decoded images around is the single biggest ingest speedup on
# large files.
# --------------------------------------------------------------------------
_DOC_CACHE: dict[str, object] = {}
_DOC_ORDER: list[str] = []
_MAX_OPEN_DOCS = 4

_QUIET_APPLIED = False


def quiet_mupdf():
    """Stop MuPDF writing straight to stderr, once per process.

    MuPDF reports recoverable problems -- damaged xref entries, inconsistent
    tagged-PDF structure trees, broken font descriptors -- by printing to
    stderr and carrying on. Real documents hit these constantly and the text
    still extracts fine, so the default behaviour is a console full of
    frightening "MuPDF error:" lines for a run that actually succeeded.

    Silencing the stream does not discard the information: the messages are
    buffered and drained by :func:`drain_mupdf_warnings` so the application
    can attribute them to a page and show them as warnings.
    """
    global _QUIET_APPLIED
    if _QUIET_APPLIED:
        return
    try:
        fitz.TOOLS.mupdf_display_errors(False)
        fitz.TOOLS.mupdf_display_warnings(False)
        fitz.TOOLS.reset_mupdf_warnings()
    except Exception:
        pass
    _QUIET_APPLIED = True


def drain_mupdf_warnings() -> list[str]:
    """Return and clear whatever MuPDF has complained about since last call."""
    try:
        raw = fitz.TOOLS.mupdf_warnings(reset=True)
    except Exception:
        return []
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        lines = [str(x).strip() for x in raw]
    else:
        lines = [ln.strip() for ln in str(raw).splitlines()]
    # Damaged files repeat the same complaint on every page; the reader only
    # needs to be told once per page what kind of problem occurred.
    seen, out = set(), []
    for line in lines:
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return out

_IMG_HASH_CACHE: dict[tuple[str, int], tuple[bytes, int]] = {}
_MAX_IMG_CACHE = 4096


def open_document(path: str, password: str = ""):
    """Open (and cache) a PDF for this process."""
    doc = _DOC_CACHE.get(path)
    if doc is not None:
        return doc
    doc = fitz.open(path)
    if doc.needs_pass:
        if not doc.authenticate(password or ""):
            doc.close()
            raise PermissionError(f"{path} is password protected")
    _DOC_CACHE[path] = doc
    _DOC_ORDER.append(path)
    while len(_DOC_ORDER) > _MAX_OPEN_DOCS:
        old = _DOC_ORDER.pop(0)
        try:
            _DOC_CACHE.pop(old).close()
        except Exception:
            pass
    return doc


def close_all_documents():
    for path in list(_DOC_CACHE):
        try:
            _DOC_CACHE.pop(path).close()
        except Exception:
            pass
    _DOC_ORDER.clear()
    _IMG_HASH_CACHE.clear()


# --------------------------------------------------------------------------
# Canonical normalisation used *only* for page fingerprints.
#
# This is intentionally independent of the user's compare settings.  Alignment
# answers "is this the same page?", and the answer should not flip because
# somebody ticked "ignore case".  Keeping it fixed also means changing compare
# settings triggers a re-diff, never a re-ingest.
# --------------------------------------------------------------------------
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def canonical_token(text: str) -> str:
    """Normalise one word for page-identity purposes.

    Purely numeric tokens are dropped.  This matters more than it looks: with
    "Page 42 of 6000" in the footer, inserting a single page changes the
    fingerprint of every page after it, so alignment finds no anchors across
    the whole tail and silently degrades to positional pairing -- exactly the
    cascade page alignment exists to prevent.

    Dropping numbers only affects *identity*.  The diff still compares every
    digit, so a changed figure is still reported as a change.
    """
    t = unicodedata.normalize("NFKC", text).casefold()
    t = _PUNCT_RE.sub("", t)
    t = _WS_RE.sub("", t)
    if not t or t.isdigit():
        return ""
    return t


# Words whose tops differ by less than this are on the same visual line.
_LINE_TOLERANCE_PT = 3.0


def reading_order(raw_words: list) -> list:
    """Order words by where they sit on the page, not by content-stream order.

    ``get_text("words")`` returns ``block_no``/``line_no``/``word_no``, and
    sorting on those looks like reading order -- but the numbering follows the
    order operators appear in the content stream, which is a property of the
    producing application, not of the page. Two PDFs that render identically
    can hand back the same words in completely different sequence, and a
    sequence diff then reports every word on the page as removed and re-added.

    Blocks are therefore ordered by geometry: top edge first, quantised to a
    line tolerance so a row of side-by-side blocks stays left-to-right, then
    left edge. Words keep their intra-block line/word order, which preserves
    column structure -- a two-column layout still reads down each column
    rather than zig-zagging across the gutter.

    The property that matters is determinism: identical pages must produce an
    identical sequence whatever produced them.
    """
    if not raw_words:
        return []

    blocks: dict[int, list] = {}
    for w in raw_words:
        blocks.setdefault(w[5], []).append(w)

    ordered: list = []
    def block_key(item):
        _bno, words = item
        top = min(w[1] for w in words)
        left = min(w[0] for w in words)
        # Quantising the top edge keeps blocks that share a visual row
        # together, sorted left to right, instead of letting sub-point
        # differences decide their order.
        return (round(top / _LINE_TOLERANCE_PT), round(left, 1), _bno)

    for _bno, words in sorted(blocks.items(), key=block_key):
        words.sort(key=lambda w: (w[6], w[7], round(w[1], 1), round(w[0], 1)))
        ordered.extend(words)
    return ordered


def _exact_hash(tokens: list[str]) -> int:
    h = hashlib.blake2b("\x1f".join(tokens).encode("utf-8"), digest_size=8)
    # SQLite integers are signed 64-bit; fold to fit.
    return int.from_bytes(h.digest(), "big", signed=True)


def _simhash(tokens: list[str]) -> int:
    """64-bit simhash over the token bag.

    Used to score "how alike are these two pages" during alignment gap filling,
    where an exact hash tells us nothing.  Hamming distance on this is a decent
    proxy for content similarity and costs one pass over the tokens.
    """
    if not tokens:
        return 0
    vec = [0] * 64
    for tok in tokens:
        hv = int.from_bytes(
            hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(),
            "big", signed=False)
        for bit in range(64):
            if hv >> bit & 1:
                vec[bit] += 1
            else:
                vec[bit] -= 1
    out = 0
    for bit in range(64):
        if vec[bit] > 0:
            out |= 1 << bit
    return out - (1 << 64) if out >= (1 << 63) else out


def simhash_similarity(a: int, b: int) -> float:
    """0.0 - 1.0 similarity from two simhashes."""
    if a == 0 and b == 0:
        return 1.0
    if a == 0 or b == 0:
        return 0.0
    ua = a & 0xFFFFFFFFFFFFFFFF
    ub = b & 0xFFFFFFFFFFFFFFFF
    return 1.0 - ((ua ^ ub).bit_count() / 64.0)


# --------------------------------------------------------------------------
# Image fingerprints
# --------------------------------------------------------------------------
# Grey levels a cell average may drift by between two encodings of the same
# picture. Cells within this band of the image mean are treated as neutral.
_HASH_NOISE_FLOOR = 6


def _perceptual_hash(pil_img) -> tuple[int, int]:
    """Return ``(hash64, mean_luminance)`` for an image.

    A plain horizontal difference hash -- each pixel against the one to its
    right -- collapses to zero for anything horizontally uniform, which
    describes a great many logos, rules and banner graphics. Every such image
    then hashed identically to every other, and zero was also being used to
    mean "no hash", so the two were indistinguishable.

    Three details matter, each learned from a case that failed:

    * The reduction uses BOX (area averaging). The default resampling filter
      aliases badly at an 8:1 reduction, so a finely striped image sampled
      slightly differently by two JPEG encoders produced completely unrelated
      hashes -- a moire artefact, not a real difference.
    * Cells are compared against the mean with a noise floor rather than
      against the median. Re-encoding moves cell averages by a few levels, and
      a bare threshold flips every cell that sits near it. Cells inside the
      floor contribute nothing on either side, so they cannot disagree.
    * The mean luminance is returned as well, because a flat image thresholds
      to all zeroes whatever its colour, and two different flat blocks must
      not compare as identical.
    """
    from PIL import Image

    small = pil_img.convert("L").resize((8, 8), Image.Resampling.BOX)
    values = list(small.getdata())
    mean = sum(values) / len(values)
    bits = 0
    for i, v in enumerate(values):
        if v > mean + _HASH_NOISE_FLOOR:
            bits |= 1 << i
    return bits, int(mean)


def image_fingerprint(doc, doc_key: str, xref: int,
                      fuzzy: bool) -> tuple[bytes, int | None, int | None]:
    """Return ``(exact_bytes_hash, perceptual_hash, mean_luminance)``.

    The perceptual values are ``None`` when the image could not be decoded, so
    "undecodable" is never mistaken for a legitimate hash of zero. Cached per
    xref: a letterhead logo repeated on 20,000 pages is decoded once.
    """
    key = (doc_key, xref)
    hit = _IMG_HASH_CACHE.get(key)
    if hit is not None:
        return hit

    exact = b""
    phash: int | None = None
    mean: int | None = None
    try:
        info = doc.extract_image(xref)
        raw = info.get("image") or b""
        exact = hashlib.blake2b(raw, digest_size=16).digest()
        if fuzzy and raw:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as im:
                im.draft("L", (64, 64))     # let JPEG decode at reduced size
                phash, mean = _perceptual_hash(im)
    except Exception:
        pass

    if len(_IMG_HASH_CACHE) > _MAX_IMG_CACHE:
        _IMG_HASH_CACHE.clear()
    _IMG_HASH_CACHE[key] = (exact, phash, mean)
    return exact, phash, mean


# Subset-embedded fonts carry a six-letter tag such as "ABCDEE+Calibri".
# The tag is generated afresh every time a file is written, so two PDFs
# showing the identical typeface report different font names -- and with
# formatting comparison on, every single word then looks restyled.
_SUBSET_TAG_RE = re.compile(r"^[A-Z]{6}\+")

# Only the bits that describe visible style. Bit 0 (superscript) and the
# serif/monospace hints are properties of the font, not of an edit.
_STYLE_FLAG_MASK = (1 << 1) | (1 << 4)          # italic | bold

# Font sizes wobble by hundredths between producers; half a point is the
# smallest difference a reader could notice.
_SIZE_QUANTUM = 0.5


# Foundry suffixes that identify the vendor's build of a typeface rather than
# the typeface itself: "Arial" and "ArialMT" are the same font, and which one
# a PDF reports depends on the producer. Style words like "Roman" are
# deliberately absent -- "Roman" is part of "TimesNewRoman", and stripping it
# merges genuinely different families.
_FOUNDRY_SUFFIX_RE = re.compile(r"(PSMT|MT|PS)$")


def normalise_font_name(name: str) -> str:
    """Strip the arbitrary parts of an embedded font name.

    Two things get removed: the six-character subset tag, which the PDF
    specification defines as arbitrary and which is regenerated on every save,
    and the foundry suffix. Real files pair "Arial" against "ArialMT" for the
    identical typeface, and treating those as a font change made every word on
    every page report as restyled.
    """
    if not name:
        return ""
    name = _SUBSET_TAG_RE.sub("", name)
    name = name.replace("-", "").replace(" ", "").replace("_", "")
    name = _FOUNDRY_SUFFIX_RE.sub("", name)
    return name.casefold()


# Two colours closer than this in RGB are the same colour to a reader. Real
# documents encode body text as #231A20 in one file and #030406 in the other;
# both are black, and reporting that as a formatting change buried every page
# under thousands of highlights.
_COLOUR_TOLERANCE = 40


def colours_differ(a: int, b: int) -> bool:
    """Is the difference between two packed RGB values actually visible?"""
    if a == b:
        return False
    ar, ag, ab = (a >> 16) & 255, (a >> 8) & 255, a & 255
    br, bg, bb = (b >> 16) & 255, (b >> 8) & 255, b & 255
    return (abs(ar - br) > _COLOUR_TOLERANCE
            or abs(ag - bg) > _COLOUR_TOLERANCE
            or abs(ab - bb) > _COLOUR_TOLERANCE)


def quantise_size(size) -> float:
    try:
        return round(float(size) / _SIZE_QUANTUM) * _SIZE_QUANTUM
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------
# Style index (only built when formatting comparison is on)
# --------------------------------------------------------------------------
def _build_style_index(page):
    """Bucket text spans by vertical position for cheap word -> style lookup.

    A linear scan of every span for every word is O(words x spans), which at
    20k pages is measurably awful.  Bucketing by a 4pt vertical band turns it
    into a handful of comparisons per word.
    """
    buckets: dict[int, list] = {}
    try:
        data = page.get_text("dict")
    except Exception:
        return buckets
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                bbox = span.get("bbox")
                if not bbox:
                    continue
                entry = (bbox, normalise_font_name(span.get("font", "")),
                         quantise_size(span.get("size", 0.0)),
                         int(span.get("color", 0)),
                         int(span.get("flags", 0)) & _STYLE_FLAG_MASK)
                lo = int(bbox[1] // 4)
                hi = int(bbox[3] // 4)
                for b in range(lo, hi + 1):
                    buckets.setdefault(b, []).append(entry)
    return buckets


def _lookup_style(buckets, x0, y0, x1, y1):
    if not buckets:
        return None
    cx = (x0 + x1) * 0.5
    cy = (y0 + y1) * 0.5
    base = int(cy // 4)
    for b in (base, base - 1, base + 1):
        for bbox, font, size, color, flags in buckets.get(b, ()):
            if bbox[0] - 0.5 <= cx <= bbox[2] + 0.5 and \
               bbox[1] - 0.5 <= cy <= bbox[3] + 0.5:
                return (font, size, color, flags)
    return None


# --------------------------------------------------------------------------
# The worker entry point
# --------------------------------------------------------------------------
def ingest_chunk(task: dict) -> dict:
    """Extract a contiguous run of pages.

    ``task`` keys: ``path``, ``side``, ``start``, ``end`` (exclusive),
    ``password``, ``compare_images``, ``image_fuzzy``, ``compare_formatting``.

    Returns ``{"side": int, "rows": [...], "errors": [(pno, msg)]}`` where each
    row is ready for :meth:`ResultStore.write_pages`.
    """
    from .store import pack

    quiet_mupdf()
    path = task["path"]
    side = task["side"]
    want_images = task.get("compare_images", True)
    fuzzy = task.get("image_fuzzy", True)
    want_format = task.get("compare_formatting", False)

    rows: list[tuple] = []
    errors: list[tuple[int, str]] = []

    try:
        doc = open_document(path, task.get("password", ""))
    except Exception as exc:
        return {"side": side, "rows": [],
                "errors": [(p, str(exc))
                           for p in range(task["start"], task["end"])]}

    for pno in range(task["start"], min(task["end"], doc.page_count)):
        try:
            page = doc.load_page(pno)
            rect = page.rect
            width, height = float(rect.width), float(rect.height)
            rotation = int(page.rotation or 0)

            # Word coordinates come back in unrotated page space, while
            # page.rect and the rendered pixmap are both in rotated (displayed)
            # space. On a 90 or 270 degree page those disagree completely --
            # the page renders 842x595 while boxes are still measured against
            # 595x842 -- so every highlight lands somewhere else entirely.
            # Mapping the boxes through the rotation matrix here means every
            # consumer downstream works in one consistent space.
            rot_matrix = page.rotation_matrix if rotation else None

            raw_words = reading_order(page.get_text("words"))

            style_buckets = _build_style_index(page) if want_format else None
            styles: list[tuple] = []
            style_ids: dict[tuple, int] = {}

            words: list[tuple] = []
            canon: list[str] = []
            for w in raw_words:
                x0, y0, x1, y1, text = w[0], w[1], w[2], w[3], w[4]
                if not text:
                    continue
                if rot_matrix is not None:
                    r = fitz.Rect(x0, y0, x1, y1) * rot_matrix
                    x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
                sid = -1
                if style_buckets is not None:
                    # Span index is built from unrotated coordinates, so it
                    # must be queried with the original box.
                    st = _lookup_style(style_buckets, w[0], w[1], w[2], w[3])
                    if st is not None:
                        sid = style_ids.get(st, -1)
                        if sid == -1:
                            sid = len(styles)
                            style_ids[st] = sid
                            styles.append(st)
                words.append((round(x0, 2), round(y0, 2), round(x1, 2),
                              round(y1, 2), text, sid))
                ct = canonical_token(text)
                if ct:
                    canon.append(ct)

            images: list[tuple] = []
            if want_images:
                seen_xrefs = set()
                for info in page.get_images(full=True):
                    xref = info[0]
                    if xref in seen_xrefs:
                        continue
                    seen_xrefs.add(xref)
                    exact, phash, mean = image_fingerprint(
                        doc, path, xref, fuzzy)
                    try:
                        rects = page.get_image_rects(xref)
                    except Exception:
                        rects = []
                    for r in rects:
                        if rot_matrix is not None:
                            r = fitz.Rect(r) * rot_matrix
                        images.append((round(float(r.x0), 2), round(float(r.y0), 2),
                                       round(float(r.x1), 2), round(float(r.y1), 2),
                                       exact, phash, xref, mean))

            # Recoverable MuPDF complaints are attributed to the page that
            # produced them, but never treated as a failure: the page has
            # already been parsed successfully by this point.
            for note in drain_mupdf_warnings():
                errors.append((pno, note))

            payload = {"w": words, "s": styles, "i": images}
            rows.append((side, pno, width, height, rotation,
                         len(words), len(images),
                         _exact_hash(canon), _simhash(canon),
                         pack(payload)))
        except Exception as exc:      # one bad page must not kill the chunk
            errors.append((pno, f"{type(exc).__name__}: {exc}"))
            rows.append((side, pno, 612.0, 792.0, 0, 0, 0, 0, 0,
                         pack({"w": [], "s": [], "i": [], "error": str(exc)})))

    return {"side": side, "rows": rows, "errors": errors}


def probe_document(path: str, password: str = "") -> dict:
    """Cheap metadata read used by the home screen before a run starts."""
    quiet_mupdf()
    out = {"path": path, "page_count": 0, "title": "", "author": "",
           "producer": "", "file_size": 0, "encrypted": False,
           "fingerprint": "", "error": ""}
    try:
        out["file_size"] = os.path.getsize(path)
    except OSError:
        pass
    doc = None
    try:
        doc = fitz.open(path)
        out["encrypted"] = bool(doc.needs_pass)
        if doc.needs_pass and not doc.authenticate(password or ""):
            out["error"] = "Password protected"
            return out
        out["page_count"] = doc.page_count
        md = doc.metadata or {}
        out["title"] = md.get("title") or ""
        out["author"] = md.get("author") or ""
        out["producer"] = md.get("producer") or ""
        out["fingerprint"] = file_fingerprint(path)
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
    return out


def file_fingerprint(path: str) -> str:
    """Identity hash used to decide whether a cached run still applies.

    Hashing a 2 GB PDF end to end just to answer "is this the same file" is
    wasteful, so this samples the head, the tail and the size -- enough to
    catch every realistic edit while staying constant-time.
    """
    h = hashlib.blake2b(digest_size=16)
    try:
        size = os.path.getsize(path)
        h.update(str(size).encode())
        with open(path, "rb") as fh:
            h.update(fh.read(1 << 20))
            if size > (1 << 21):
                fh.seek(-(1 << 20), 2)
                h.update(fh.read(1 << 20))
    except OSError:
        return ""
    return h.hexdigest()
