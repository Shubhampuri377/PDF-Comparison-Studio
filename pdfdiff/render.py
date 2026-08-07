"""Page rasterisation.

Deliberately Qt-free: the viewer, the thumbnail strip and the report writers
all rasterise through here.

Two things this module does differently from the old engine:

* PyMuPDF renders the pages, so Poppler and its ~100 MB of bundled binaries
  are gone along with the subprocess-per-page cost.
* Highlights are returned as geometry, not baked into the bitmap.  The viewer
  paints them as vector items, so zooming never re-runs a diff or re-encodes
  an image -- which is what made the old zoom path allocate without bound.
"""
from __future__ import annotations

import threading

try:
    import pymupdf as fitz
except ImportError:                      # pragma: no cover
    import fitz

from .models import (
    CH_ADDED, CH_CHANGED, CH_FORMAT, CH_IMG_ADDED, CH_IMG_CHANGED,
    CH_IMG_MOVED, CH_IMG_REMOVED, CH_MOVED, CH_REMOVED,
)

PDF_POINTS_PER_INCH = 72.0

# Highlight palette.  RGB plus a fill alpha; the viewer strokes the same hue at
# full opacity so a thin box is still visible against dense text.
HIGHLIGHT_COLOURS: dict[str, tuple[int, int, int, int]] = {
    CH_REMOVED:     (232,  62,  74, 70),
    CH_ADDED:       ( 34, 178, 110, 70),
    CH_CHANGED:     (240, 158,  30, 80),
    CH_MOVED:       ( 86, 128, 233, 60),
    CH_FORMAT:      (167,  95, 224, 60),
    CH_IMG_REMOVED: (232,  62,  74, 45),
    CH_IMG_ADDED:   ( 34, 178, 110, 45),
    CH_IMG_MOVED:   ( 86, 128, 233, 45),
    CH_IMG_CHANGED: (240, 158,  30, 45),
}


class RenderedPage:
    """Raw RGB888 samples plus the geometry needed to place highlights."""

    __slots__ = ("samples", "width", "height", "stride", "scale",
                 "page_width", "page_height")

    def __init__(self, samples: bytes, width: int, height: int, stride: int,
                 scale: float, page_width: float, page_height: float):
        self.samples = samples
        self.width = width
        self.height = height
        self.stride = stride
        self.scale = scale
        self.page_width = page_width
        self.page_height = page_height

    @property
    def nbytes(self) -> int:
        return len(self.samples)


class PageRenderer:
    """Per-thread PDF handles.

    ``fitz.Document`` is not safe to share between threads, so each thread that
    renders gets its own handles, created lazily and closed together.
    """

    def __init__(self, path_a: str, path_b: str,
                 password_a: str = "", password_b: str = ""):
        self.paths = (path_a, path_b)
        self.passwords = (password_a, password_b)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._all: list = []

    def _doc(self, side: int):
        from .extract import quiet_mupdf

        quiet_mupdf()
        docs = getattr(self._local, "docs", None)
        if docs is None:
            docs = {}
            self._local.docs = docs
        doc = docs.get(side)
        if doc is None:
            doc = fitz.open(self.paths[side])
            if doc.needs_pass:
                doc.authenticate(self.passwords[side] or "")
            docs[side] = doc
            with self._lock:
                self._all.append(doc)
        return doc

    def page_size(self, side: int, pno: int) -> tuple[float, float]:
        doc = self._doc(side)
        if pno is None or pno < 0 or pno >= doc.page_count:
            return 612.0, 792.0
        r = doc.load_page(pno).rect
        return float(r.width), float(r.height)

    def render(self, side: int, pno: int, scale: float = 1.0,
               max_pixels: int = 40_000_000) -> RenderedPage | None:
        """Rasterise one page at ``scale`` (1.0 == 72 dpi).

        ``max_pixels`` is a hard ceiling.  Without it a deep zoom on an A0
        drawing asks for a multi-gigabyte bitmap and the process dies; the old
        UI had exactly this failure mode on its unbounded zoom button.
        """
        if pno is None:
            return None
        doc = self._doc(side)
        if pno < 0 or pno >= doc.page_count:
            return None
        page = doc.load_page(pno)
        rect = page.rect
        pw, ph = float(rect.width), float(rect.height)

        est = (pw * scale) * (ph * scale)
        if est > max_pixels and est > 0:
            scale = scale * (max_pixels / est) ** 0.5

        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return RenderedPage(pix.samples, pix.width, pix.height, pix.stride,
                            scale, pw, ph)

    def render_thumbnail(self, side: int, pno: int,
                         max_px: int = 132) -> RenderedPage | None:
        if pno is None:
            return None
        pw, ph = self.page_size(side, pno)
        if pw <= 0 or ph <= 0:
            return None
        scale = max_px / max(pw, ph)
        return self.render(side, pno, scale)

    def close(self):
        with self._lock:
            for doc in self._all:
                try:
                    doc.close()
                except Exception:
                    pass
            self._all.clear()


# --------------------------------------------------------------------------
# PIL compositing, used by the report writers
# --------------------------------------------------------------------------
def to_pil(rendered: RenderedPage):
    from PIL import Image

    return Image.frombytes("RGB", (rendered.width, rendered.height),
                           rendered.samples, "raw", "RGB", rendered.stride)


def paint_highlights(image, rendered: RenderedPage, boxes, side: int,
                     outline: bool = True):
    """Composite highlight rectangles onto a rendered page image."""
    from PIL import Image, ImageDraw

    relevant = [b for b in boxes if b[1] == side]
    if not relevant:
        return image

    scale = rendered.scale
    overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for category, _side, x0, y0, x1, y1, _text, _detail in relevant:
        colour = HIGHLIGHT_COLOURS.get(category)
        if colour is None:
            continue
        r, g, b, a = colour
        box = [x0 * scale - 1, y0 * scale - 1, x1 * scale + 1, y1 * scale + 1]
        if box[2] <= box[0]:
            box[2] = box[0] + 1
        if box[3] <= box[1]:
            box[3] = box[1] + 1
        draw.rectangle(box, fill=(r, g, b, a),
                       outline=(r, g, b, 235) if outline else None, width=1)
    return Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")


def compose_side_by_side(left, right, gap: int = 16,
                         background=(255, 255, 255),
                         divider=(214, 219, 227)):
    """Glue two page images together at a common height."""
    from PIL import Image

    if left is None and right is None:
        return None
    if left is None:
        left = Image.new("RGB", right.size, background)
    if right is None:
        right = Image.new("RGB", left.size, background)

    height = max(left.height, right.height)
    lw = max(1, round(left.width * height / left.height))
    rw = max(1, round(right.width * height / right.height))
    left = left.resize((lw, height), Image.Resampling.LANCZOS)
    right = right.resize((rw, height), Image.Resampling.LANCZOS)

    canvas = Image.new("RGB", (lw + rw + gap, height), background)
    canvas.paste(left, (0, 0))
    canvas.paste(right, (lw + gap, 0))
    for x in range(lw + gap // 2 - 1, lw + gap // 2 + 1):
        for y in range(height):
            canvas.putpixel((x, y), divider)
    return canvas


def mask_regions(image, rendered: RenderedPage, fractions,
                 label: str = "excluded from comparison"):
    """Cover excluded regions so a report only shows what was actually checked.

    Painted opaque rather than tinted: a reviewer reading the report should
    not be able to mistake un-compared content for content that was compared
    and found identical. The hatching and caption make the omission
    deliberate and obvious instead of looking like a rendering fault.
    """
    from PIL import Image, ImageDraw

    if not fractions:
        return image

    out = image.convert("RGB")
    draw = ImageDraw.Draw(out)
    w, h = out.size
    for fx0, fy0, fx1, fy1 in fractions:
        x0, y0 = int(fx0 * w), int(fy0 * h)
        x1, y1 = int(fx1 * w), int(fy1 * h)
        if x1 <= x0 or y1 <= y0:
            continue
        draw.rectangle([x0, y0, x1, y1], fill=(242, 244, 247),
                       outline=(176, 184, 196), width=2)
        step = max(10, (y1 - y0) // 14)
        for offset in range(0, (x1 - x0) + (y1 - y0), step):
            draw.line([(x0 + offset, y0), (x0, y0 + offset)],
                      fill=(214, 220, 229), width=1)
        if label and (x1 - x0) > 140 and (y1 - y0) > 22:
            draw.text((x0 + 8, y0 + 6), label, fill=(120, 130, 145))
    return out


def render_pair_image(renderer: PageRenderer, a_pno, b_pno, boxes,
                      scale: float = 1.0, max_width: int | None = None,
                      mask_a=None, mask_b=None):
    """Full side-by-side composite with highlights, for reports."""
    from PIL import Image

    left = right = None
    ra = renderer.render(0, a_pno, scale) if a_pno is not None else None
    rb = renderer.render(1, b_pno, scale) if b_pno is not None else None
    if ra is not None:
        left = to_pil(ra)
        if mask_a:
            left = mask_regions(left, ra, mask_a)
        left = paint_highlights(left, ra, boxes, 0)
    if rb is not None:
        right = to_pil(rb)
        if mask_b:
            right = mask_regions(right, rb, mask_b)
        right = paint_highlights(right, rb, boxes, 1)

    composite = compose_side_by_side(left, right)
    if composite is not None and max_width and composite.width > max_width:
        h = round(composite.height * max_width / composite.width)
        composite = composite.resize((max_width, h), Image.Resampling.LANCZOS)
    return composite
