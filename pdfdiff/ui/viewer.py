"""Side-by-side page viewer.

Built on ``QGraphicsView`` so zoom is a view transform rather than a bitmap
resample.  Highlights are vector items in page-point coordinates, which means
they stay crisp at any zoom and cost nothing to redraw -- the old implementation
re-composited a full-page PIL image on every zoom step, unbounded, which is
what made deep zoom eat memory.
"""
from __future__ import annotations

from PySide6.QtCore import (QEvent, QPointF, QRectF, Qt, QTimer, Signal)
from PySide6.QtGui import (QBrush, QColor, QCursor, QPainter, QPen, QPixmap,
                           QTransform)
from PySide6.QtWidgets import (QFrame, QGraphicsItem, QGraphicsPixmapItem,
                               QGraphicsRectItem, QGraphicsScene,
                               QGraphicsSimpleTextItem, QGraphicsView,
                               QHBoxLayout, QLabel, QSizePolicy, QVBoxLayout,
                               QWidget)

from ..render import HIGHLIGHT_COLOURS
from .theme import Palette

MODE_SPLIT = "split"
MODE_LEFT = "left"
MODE_RIGHT = "right"
MODE_OVERLAY = "overlay"

MIN_ZOOM = 0.08
MAX_ZOOM = 12.0

# Rasterise at this multiple of the on-screen zoom so text stays sharp when the
# view scales the pixmap slightly, without re-rendering on every wheel notch.
_RENDER_STEPS = (0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0)


def _render_scale_for(zoom: float) -> float:
    for step in _RENDER_STEPS:
        if zoom <= step:
            return step
    return _RENDER_STEPS[-1]


class PageScene(QGraphicsScene):
    """One document page plus its highlight overlay."""

    def __init__(self, palette: Palette, side: int, parent=None):
        super().__init__(parent)
        self.p = palette
        self.side = side
        self.setBackgroundBrush(QColor(palette.canvas))

        # A white sheet behind the page bitmap. Overlay mode blends two
        # semi-transparent pages together; without an opaque backdrop the
        # blend lands on the dark canvas colour and both pages come out grey.
        self._page_bg = QGraphicsRectItem()
        self._page_bg.setBrush(QBrush(QColor(255, 255, 255)))
        self._page_bg.setPen(Qt.PenStyle.NoPen)
        self._page_bg.setZValue(-10)
        self._page_bg.setVisible(False)
        self.addItem(self._page_bg)

        self._pixmap_item = QGraphicsPixmapItem()
        self._pixmap_item.setTransformationMode(
            Qt.TransformationMode.SmoothTransformation)
        self.addItem(self._pixmap_item)

        self._highlights: list[QGraphicsRectItem] = []
        self._ignore_items: list[QGraphicsRectItem] = []
        self._placeholder: QGraphicsSimpleTextItem | None = None
        self.page_width = 612.0
        self.page_height = 792.0
        self._render_scale = 1.0

    def apply_palette(self, palette: Palette):
        self.p = palette
        self.setBackgroundBrush(QColor(palette.canvas))
        if self._placeholder is not None:
            self._placeholder.setBrush(QBrush(QColor(palette.text_faint)))
        for item in self._ignore_items:
            pen = QPen(QColor(palette.text_dim), 0.0, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            item.setPen(pen)
        self.update()

    # -- content -----------------------------------------------------------
    def set_pixmap(self, pm: QPixmap | None, page_w: float, page_h: float,
                   render_scale: float, show_sheet: bool = False):
        self.page_width = page_w or 612.0
        self.page_height = page_h or 792.0
        self._render_scale = render_scale or 1.0
        if pm is None or pm.isNull():
            self._pixmap_item.setPixmap(QPixmap())
            self._pixmap_item.setVisible(False)
        else:
            self._pixmap_item.setPixmap(pm)
            self._pixmap_item.setVisible(True)
            # Normalise to page points so highlights need no conversion.
            inv = 1.0 / self._render_scale
            self._pixmap_item.setTransform(QTransform().scale(inv, inv))
        self._page_bg.setRect(QRectF(0, 0, self.page_width, self.page_height))
        self._page_bg.setVisible(show_sheet)
        self.setSceneRect(0, 0, self.page_width, self.page_height)

    def set_sheet_visible(self, visible: bool):
        self._page_bg.setVisible(visible)

    def show_placeholder(self, text: str):
        self.clear_highlights()
        self._pixmap_item.setVisible(False)
        if self._placeholder is None:
            self._placeholder = QGraphicsSimpleTextItem()
            self._placeholder.setBrush(QBrush(QColor(self.p.text_faint)))
            font = self._placeholder.font()
            font.setPointSize(11)
            self._placeholder.setFont(font)
            self.addItem(self._placeholder)
        self._placeholder.setText(text)
        self._placeholder.setVisible(True)
        rect = self._placeholder.boundingRect()
        self._placeholder.setPos(
            (self.page_width - rect.width()) / 2,
            (self.page_height - rect.height()) / 2)

    def hide_placeholder(self):
        if self._placeholder is not None:
            self._placeholder.setVisible(False)

    # -- highlights --------------------------------------------------------
    def clear_highlights(self):
        for item in self._highlights:
            self.removeItem(item)
        self._highlights.clear()

    def set_highlights(self, boxes, enabled_categories: set[str],
                       sides: tuple[int, ...] | None = None):
        """Paint highlight rectangles.

        ``sides`` defaults to this scene's own side. Overlay mode passes both,
        because it stacks document B on top of A in the same scene -- and an
        added page carries all of its highlights on the B side, so restricting
        to side A would render the overlay with no highlights at all.
        """
        if sides is None:
            sides = (self.side,)
        self.clear_highlights()
        for category, side, x0, y0, x1, y1, _text, _detail in boxes:
            if side not in sides or category not in enabled_categories:
                continue
            colour = HIGHLIGHT_COLOURS.get(category)
            if colour is None:
                continue
            r, g, b, a = colour
            item = QGraphicsRectItem(
                QRectF(x0 - 0.6, y0 - 0.6,
                       max(1.0, x1 - x0) + 1.2, max(1.0, y1 - y0) + 1.2))
            item.setBrush(QBrush(QColor(r, g, b, a)))
            pen = QPen(QColor(r, g, b, 230), 0.0)
            pen.setCosmetic(True)          # constant 1px stroke at any zoom
            item.setPen(pen)
            item.setZValue(10)
            self.addItem(item)
            self._highlights.append(item)

    # -- ignore regions ----------------------------------------------------
    def set_ignore_rects(self, fractions):
        for item in self._ignore_items:
            self.removeItem(item)
        self._ignore_items.clear()
        for fx0, fy0, fx1, fy1 in fractions:
            rect = QRectF(fx0 * self.page_width, fy0 * self.page_height,
                          (fx1 - fx0) * self.page_width,
                          (fy1 - fy0) * self.page_height)
            item = QGraphicsRectItem(rect)
            item.setBrush(QBrush(QColor(120, 132, 150, 46)))
            pen = QPen(QColor(self.p.text_dim), 0.0, Qt.PenStyle.DashLine)
            pen.setCosmetic(True)
            item.setPen(pen)
            item.setZValue(20)
            self.addItem(item)
            self._ignore_items.append(item)


class PageView(QGraphicsView):
    """Zoom/pan view for one page.  Emits its transform so the twin can sync."""

    transform_changed = Signal(float, QPointF)
    region_drawn = Signal(float, float, float, float)   # page fractions
    zoom_requested = Signal(float, QPointF)             # factor, view pos

    def __init__(self, scene: PageScene, palette: Palette, parent=None):
        super().__init__(scene, parent)
        self.p = palette
        self.setRenderHints(QPainter.RenderHint.Antialiasing
                            | QPainter.RenderHint.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(
            QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setViewportUpdateMode(
            QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

        self._draw_mode = False
        self._draw_origin: QPointF | None = None
        self._draw_item: QGraphicsRectItem | None = None
        self._syncing = False

    def apply_palette(self, palette: Palette):
        self.p = palette

    # -- ignore drawing ----------------------------------------------------
    def set_draw_mode(self, on: bool):
        self._draw_mode = on
        self.setDragMode(QGraphicsView.DragMode.NoDrag if on
                         else QGraphicsView.DragMode.ScrollHandDrag)
        self.setCursor(QCursor(Qt.CursorShape.CrossCursor if on
                               else Qt.CursorShape.ArrowCursor))

    def mousePressEvent(self, event):
        if self._draw_mode and event.button() == Qt.MouseButton.LeftButton:
            self._draw_origin = self.mapToScene(event.position().toPoint())
            self._draw_item = QGraphicsRectItem(
                QRectF(self._draw_origin, self._draw_origin))
            self._draw_item.setBrush(QBrush(QColor(79, 140, 255, 52)))
            pen = QPen(QColor(self.p.accent), 0.0)
            pen.setCosmetic(True)
            self._draw_item.setPen(pen)
            self._draw_item.setZValue(30)
            self.scene().addItem(self._draw_item)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._draw_mode and self._draw_item is not None:
            current = self.mapToScene(event.position().toPoint())
            self._draw_item.setRect(
                QRectF(self._draw_origin, current).normalized())
            event.accept()
            return
        super().mouseMoveEvent(event)
        if not self._syncing and self.dragMode() == \
                QGraphicsView.DragMode.ScrollHandDrag and event.buttons():
            self._emit_transform()

    def mouseReleaseEvent(self, event):
        if self._draw_mode and self._draw_item is not None:
            rect = self._draw_item.rect().normalized()
            self.scene().removeItem(self._draw_item)
            self._draw_item = None
            self._draw_origin = None
            scene: PageScene = self.scene()
            if rect.width() > 3 and rect.height() > 3:
                self.region_drawn.emit(
                    max(0.0, rect.left() / scene.page_width),
                    max(0.0, rect.top() / scene.page_height),
                    min(1.0, rect.right() / scene.page_width),
                    min(1.0, rect.bottom() / scene.page_height))
            event.accept()
            return
        super().mouseReleaseEvent(event)
        self._emit_transform()

    # -- zoom --------------------------------------------------------------
    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                self.zoom_requested.emit(1.0015 ** delta,
                                         event.position())
            event.accept()
            return
        super().wheelEvent(event)
        self._emit_transform()

    def apply_zoom(self, zoom: float):
        self.setTransform(QTransform().scale(zoom, zoom))

    def current_zoom(self) -> float:
        return self.transform().m11()

    def _emit_transform(self):
        if self._syncing:
            return
        self.transform_changed.emit(
            self.current_zoom(),
            QPointF(self.horizontalScrollBar().value(),
                    self.verticalScrollBar().value()))

    def sync_to(self, zoom: float, scroll: QPointF):
        self._syncing = True
        try:
            if abs(self.current_zoom() - zoom) > 1e-6:
                self.apply_zoom(zoom)
            self.horizontalScrollBar().setValue(int(scroll.x()))
            self.verticalScrollBar().setValue(int(scroll.y()))
        finally:
            self._syncing = False

    def scrollContentsBy(self, dx, dy):
        super().scrollContentsBy(dx, dy)
        self._emit_transform()


# --------------------------------------------------------------------------
class ComparisonViewer(QWidget):
    """Two synchronised page views plus an overlay mode."""

    zoom_changed = Signal(float)
    region_drawn = Signal(int, float, float, float, float)   # side + fractions
    render_needed = Signal(float)                            # render scale

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.p = palette
        self._mode = MODE_SPLIT
        self._zoom = 1.0
        self._sync = True
        self._boxes: list = []
        self._enabled_categories: set[str] = set(HIGHLIGHT_COLOURS)
        self._render_scale = 1.0
        self._overlay_opacity = 0.5
        self._pm_a = None
        self._pm_b = None
        self._missing_a = ""
        self._missing_b = ""

        self.scene_a = PageScene(palette, 0, self)
        self.scene_b = PageScene(palette, 1, self)
        self.view_a = PageView(self.scene_a, palette, self)
        self.view_b = PageView(self.scene_b, palette, self)

        # Overlay mode reuses scene A and stacks B's pixmap on top of it.
        self._overlay_item = QGraphicsPixmapItem()
        self._overlay_item.setZValue(5)
        self._overlay_item.setOpacity(self._overlay_opacity)
        self._overlay_item.setVisible(False)
        self._overlay_item.setTransformationMode(
            Qt.TransformationMode.SmoothTransformation)
        self.scene_a.addItem(self._overlay_item)

        for view, other in ((self.view_a, self.view_b),
                            (self.view_b, self.view_a)):
            view.transform_changed.connect(
                lambda z, s, o=other: self._on_transform(z, s, o))
            view.zoom_requested.connect(self._on_zoom_requested)
        self.view_a.region_drawn.connect(
            lambda *r: self.region_drawn.emit(0, *r))
        self.view_b.region_drawn.connect(
            lambda *r: self.region_drawn.emit(1, *r))

        self._caption_left_text = "Document A"
        self._caption_right_text = "Document B"
        self._caption_a = self._make_caption(self._caption_left_text)
        self._caption_b = self._make_caption(self._caption_right_text)

        col_a = QVBoxLayout()
        col_a.setContentsMargins(0, 0, 0, 0)
        col_a.setSpacing(0)
        col_a.addWidget(self._caption_a)
        col_a.addWidget(self.view_a, 1)
        self._wrap_a = QWidget()
        self._wrap_a.setLayout(col_a)

        col_b = QVBoxLayout()
        col_b.setContentsMargins(0, 0, 0, 0)
        col_b.setSpacing(0)
        col_b.addWidget(self._caption_b)
        col_b.addWidget(self.view_b, 1)
        self._wrap_b = QWidget()
        self._wrap_b.setLayout(col_b)

        self._divider = QFrame()
        self._divider.setObjectName("Separator")
        self._divider.setFixedWidth(1)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._wrap_a, 1)
        lay.addWidget(self._divider)
        lay.addWidget(self._wrap_b, 1)

        # Debounce re-rasterisation so a zoom gesture renders once at the end.
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(160)
        self._render_timer.timeout.connect(self._maybe_request_render)

    def _make_caption(self, text: str) -> QLabel:
        lb = QLabel(text)
        lb.setObjectName("SectionLabel")
        lb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lb.setStyleSheet(self._caption_style())
        return lb

    def _caption_style(self) -> str:
        return (f"background:{self.p.panel}; color:{self.p.text_dim};"
                f"border-bottom:1px solid {self.p.line}; padding:5px 0;")

    def apply_palette(self, palette: Palette):
        # Child scenes and views are found by the tree walker on their own;
        # this only has to handle what this widget owns directly.
        self.p = palette
        for caption in (self._caption_a, self._caption_b):
            caption.setStyleSheet(self._caption_style())

    # -- captions ----------------------------------------------------------
    def set_captions(self, left: str, right: str):
        self._caption_left_text = left
        self._caption_right_text = right
        self._refresh_captions()

    def _refresh_captions(self):
        if self._mode == MODE_OVERLAY:
            note = self.overlay_state()
            base = (f"Overlay — {self._caption_left_text} under "
                    f"{self._caption_right_text}")
            self._caption_a.setText(f"{base}   ·   {note}" if note else base)
        else:
            self._caption_a.setText(self._caption_left_text)
        self._caption_b.setText(self._caption_right_text)

    # -- mode --------------------------------------------------------------
    def set_mode(self, mode: str):
        self._mode = mode
        split = mode == MODE_SPLIT
        overlay = mode == MODE_OVERLAY
        self._wrap_a.setVisible(mode in (MODE_SPLIT, MODE_LEFT, MODE_OVERLAY))
        self._wrap_b.setVisible(mode in (MODE_SPLIT, MODE_RIGHT))
        self._divider.setVisible(split)
        self._overlay_item.setVisible(overlay)
        self.scene_a.set_sheet_visible(overlay)
        # Both of these depend on the mode, so switching has to redo them.
        self._refresh_placeholders()
        self.apply_highlights()
        self._refresh_captions()

    def mode(self) -> str:
        return self._mode

    def set_overlay_opacity(self, value: float):
        self._overlay_opacity = max(0.0, min(1.0, value))
        if getattr(self, "_pm_a", None) is not None and \
                getattr(self, "_pm_b", None) is not None:
            self._overlay_item.setOpacity(self._overlay_opacity)

    def set_sync(self, on: bool):
        self._sync = on

    # -- content -----------------------------------------------------------
    def set_pages(self, pm_a: QPixmap | None, pm_b: QPixmap | None,
                  size_a: tuple[float, float], size_b: tuple[float, float],
                  render_scale: float, boxes, missing_a: str = "",
                  missing_b: str = ""):
        self._render_scale = render_scale
        self._boxes = boxes or []
        self._pm_a, self._pm_b = pm_a, pm_b
        self._missing_a, self._missing_b = missing_a, missing_b

        overlay = self._mode == MODE_OVERLAY
        # In overlay the A scene hosts both pages, so it needs the page-sized
        # white sheet and, when A itself is absent, the geometry of B.
        geom = size_a if pm_a is not None else size_b
        self.scene_a.set_pixmap(pm_a, geom[0], geom[1], render_scale,
                                show_sheet=overlay)
        self.scene_b.set_pixmap(pm_b, size_b[0], size_b[1], render_scale)

        if pm_b is not None:
            self._overlay_item.setPixmap(pm_b)
            inv = 1.0 / (render_scale or 1.0)
            self._overlay_item.setTransform(QTransform().scale(inv, inv))
        else:
            self._overlay_item.setPixmap(QPixmap())

        self._refresh_placeholders()
        self.apply_highlights()
        # The overlay caption reports whether there is anything to blend, so
        # it has to follow page navigation, not just mode changes.
        self._refresh_captions()

    def _refresh_placeholders(self):
        """Show 'page missing' text only where it is actually informative.

        In overlay the two pages share one scene, so a placeholder for the
        absent side would print straight across the page that *is* there.
        """
        pm_a = getattr(self, "_pm_a", None)
        pm_b = getattr(self, "_pm_b", None)
        overlay = self._mode == MODE_OVERLAY

        if overlay:
            if pm_a is None and pm_b is None:
                self.scene_a.show_placeholder("No page on either side")
            elif pm_a is None:
                # Only B exists: there is nothing to blend, so show it solid.
                self.scene_a.hide_placeholder()
                self._overlay_item.setOpacity(1.0)
            elif pm_b is None:
                self.scene_a.hide_placeholder()
                self._overlay_item.setOpacity(0.0)
            else:
                self.scene_a.hide_placeholder()
                self._overlay_item.setOpacity(self._overlay_opacity)
        else:
            if pm_a is None:
                self.scene_a.show_placeholder(
                    self._missing_a or "No matching page in A")
            else:
                self.scene_a.hide_placeholder()

        if pm_b is None:
            self.scene_b.show_placeholder(
                self._missing_b or "No matching page in B")
        else:
            self.scene_b.hide_placeholder()

    def overlay_state(self) -> str:
        """Human-readable note for the caption when blending is degenerate."""
        pm_a = getattr(self, "_pm_a", None)
        pm_b = getattr(self, "_pm_b", None)
        if pm_a is None and pm_b is not None:
            return "only in B — nothing to blend against"
        if pm_b is None and pm_a is not None:
            return "only in A — nothing to blend against"
        return ""

    def set_boxes(self, boxes):
        self._boxes = boxes or []
        self.apply_highlights()

    def set_enabled_categories(self, categories: set[str]):
        self._enabled_categories = set(categories)
        self.apply_highlights()

    def apply_highlights(self):
        # Overlay stacks both documents into scene A, so scene A must draw
        # both sides' boxes or the blended view shows no highlights at all.
        sides_a = (0, 1) if self._mode == MODE_OVERLAY else (0,)
        self.scene_a.set_highlights(self._boxes, self._enabled_categories,
                                    sides_a)
        self.scene_b.set_highlights(self._boxes, self._enabled_categories,
                                    (1,))

    def set_ignore_rects(self, rects_a, rects_b):
        self.scene_a.set_ignore_rects(rects_a)
        self.scene_b.set_ignore_rects(rects_b)

    def set_draw_mode(self, on: bool):
        self.view_a.set_draw_mode(on)
        self.view_b.set_draw_mode(on)

    # -- zoom --------------------------------------------------------------
    def _on_transform(self, zoom: float, scroll: QPointF, other: PageView):
        if self._sync and self._mode == MODE_SPLIT:
            other.sync_to(zoom, scroll)
        if abs(zoom - self._zoom) > 1e-6:
            self._zoom = zoom
            self.zoom_changed.emit(zoom)
            self._render_timer.start()

    def _on_zoom_requested(self, factor: float, pos: QPointF):
        self.zoom_by(factor)

    def zoom_by(self, factor: float):
        self.set_zoom(self._zoom * factor)

    def set_zoom(self, zoom: float):
        zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        if abs(zoom - self._zoom) < 1e-6:
            return
        self._zoom = zoom
        self.view_a.apply_zoom(zoom)
        self.view_b.apply_zoom(zoom)
        self.zoom_changed.emit(zoom)
        self._render_timer.start()

    def zoom(self) -> float:
        return self._zoom

    def fit_page(self):
        view = self.view_a if self._wrap_a.isVisible() else self.view_b
        scene = view.scene()
        if scene.page_width <= 0 or scene.page_height <= 0:
            return
        vw = view.viewport().width() - 24
        vh = view.viewport().height() - 24
        if vw <= 0 or vh <= 0:
            return
        self.set_zoom(min(vw / scene.page_width, vh / scene.page_height))
        self._centre_all()

    def fit_width(self):
        view = self.view_a if self._wrap_a.isVisible() else self.view_b
        scene = view.scene()
        if scene.page_width <= 0:
            return
        vw = view.viewport().width() - 24
        if vw <= 0:
            return
        self.set_zoom(vw / scene.page_width)
        self._centre_all()

    def _centre_all(self):
        for view in (self.view_a, self.view_b):
            view.centerOn(view.scene().sceneRect().center())

    def _maybe_request_render(self):
        wanted = _render_scale_for(self._zoom)
        if abs(wanted - self._render_scale) > 1e-6:
            self.render_needed.emit(wanted)

    def required_render_scale(self) -> float:
        return _render_scale_for(self._zoom)

    # -- change navigation -------------------------------------------------
    def focus_box(self, box):
        """Centre both views on one highlight."""
        _cat, side, x0, y0, x1, y1, _t, _d = box
        target = QPointF((x0 + x1) / 2, (y0 + y1) / 2)
        for view in (self.view_a, self.view_b):
            view.centerOn(target)
