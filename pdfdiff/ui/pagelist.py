"""Virtualized page navigator.

At 20,000 pages the navigator cannot be a stack of widgets.  This is a
``QAbstractListModel`` over a plain list of row indices, with page statistics
fetched from SQLite in windows and thumbnails rendered only for rows that are
actually on screen.  Memory stays flat whether the comparison covers 20 pages
or 20,000.
"""
from __future__ import annotations

from collections import OrderedDict

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QRect, QRectF,
                            QSize, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QListView, QStyle,
                               QStyledItemDelegate)

from ..models import (CATEGORY_LABELS, IMAGE_CATEGORIES, KIND_ADDED,
                      KIND_REMOVED, PageStat, TEXT_CATEGORIES)
from ..store import ResultStore
from .theme import CATEGORY_HEX, Palette

ROW_HEIGHT = 66
THUMB_W = 38
THUMB_H = 50

StatRole = Qt.ItemDataRole.UserRole + 1
ThumbRole = Qt.ItemDataRole.UserRole + 2


class PageListModel(QAbstractListModel):
    """Rows are pair indices; everything else is fetched lazily."""

    thumb_needed = Signal(int, int, object, object)   # gen, row, a_pno, b_pno

    def __init__(self, parent=None):
        super().__init__(parent)
        self._store: ResultStore | None = None
        # Bumped on every filter change. Row indices shift when the filter
        # changes, so an in-flight thumbnail would otherwise land on whatever
        # page now occupies that row.
        self._generation = 0
        self._rows: list[int] = []
        self._stats: OrderedDict[int, PageStat] = OrderedDict()
        self._thumbs: OrderedDict[int, QPixmap] = OrderedDict()
        self._requested: set[int] = set()
        self._max_stats = 4000
        self._max_thumbs = 400
        self.thumbnails_enabled = True

    # -- wiring ------------------------------------------------------------
    def set_store(self, store: ResultStore | None):
        self._store = store
        self.set_rows([])

    def set_rows(self, rows: list[int]):
        self.beginResetModel()
        self._generation += 1
        self._rows = rows
        self._stats.clear()
        self._requested.clear()
        self.endResetModel()

    @property
    def generation(self) -> int:
        return self._generation

    def rows(self) -> list[int]:
        return self._rows

    def row_of(self, pair_idx: int) -> int:
        try:
            return self._rows.index(pair_idx)
        except ValueError:
            return -1

    def pair_index(self, row: int) -> int:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return -1

    def invalidate(self):
        """Drop cached statistics after a re-diff, keeping thumbnails."""
        self._stats.clear()
        if self._rows:
            self.dataChanged.emit(self.index(0), self.index(len(self._rows) - 1))

    # -- Qt model ----------------------------------------------------------
    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if role == StatRole:
            return self._stat(row)
        if role == ThumbRole:
            return self._thumb(row)
        if role == Qt.ItemDataRole.DisplayRole:
            stat = self._stat(row)
            return stat.label() if stat else ""
        if role == Qt.ItemDataRole.ToolTipRole:
            stat = self._stat(row)
            if not stat:
                return None
            bits = [f"<b>{stat.label()}</b>"]
            if stat.status == "identical":
                bits.append("No differences")
            for cat, n in sorted(stat.counts.items(), key=lambda kv: -kv[1]):
                bits.append(f"{CATEGORY_LABELS.get(cat, cat)}: {n:,}")
            for flag in stat.flag_labels():
                bits.append(f"<i>{flag}</i>")
            if stat.error:
                bits.append(f"<span style='color:#e83e4a'>{stat.error}</span>")
            return "<br>".join(bits)
        return None

    # -- lazy loading ------------------------------------------------------
    def _stat(self, row: int) -> PageStat | None:
        if self._store is None or not (0 <= row < len(self._rows)):
            return None
        pair_idx = self._rows[row]
        hit = self._stats.get(pair_idx)
        if hit is not None:
            self._stats.move_to_end(pair_idx)
            return hit

        # Fetch a window around the row: scrolling asks for neighbours next.
        lo = max(0, row - 40)
        hi = min(len(self._rows), row + 80)
        wanted = [self._rows[i] for i in range(lo, hi)
                  if self._rows[i] not in self._stats]
        fetched = self._store.get_stats(wanted)
        for k, v in fetched.items():
            self._stats[k] = v
        while len(self._stats) > self._max_stats:
            self._stats.popitem(last=False)
        return self._stats.get(pair_idx)

    def _thumb(self, row: int) -> QPixmap | None:
        if not self.thumbnails_enabled:
            return None
        pair_idx = self._rows[row] if 0 <= row < len(self._rows) else -1
        if pair_idx < 0:
            return None
        pm = self._thumbs.get(pair_idx)
        if pm is not None:
            self._thumbs.move_to_end(pair_idx)
            return pm
        if pair_idx not in self._requested:
            stat = self._stat(row)
            if stat is not None:
                self._requested.add(pair_idx)
                self.thumb_needed.emit(self._generation, row,
                                       stat.a_pno, stat.b_pno)
        return None

    def set_thumb(self, generation: int, row: int, pm: QPixmap | None):
        if generation != self._generation:
            return          # the filter moved on; this row is a different page
        if not (0 <= row < len(self._rows)):
            return
        pair_idx = self._rows[row]
        if pm is not None:
            self._thumbs[pair_idx] = pm
            while len(self._thumbs) > self._max_thumbs:
                dropped, _ = self._thumbs.popitem(last=False)
                self._requested.discard(dropped)
        idx = self.index(row)
        self.dataChanged.emit(idx, idx, [ThumbRole])


# --------------------------------------------------------------------------
class PageListDelegate(QStyledItemDelegate):
    """Paints one navigator row: thumbnail, label, status, change bars."""

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.p = palette

    def apply_palette(self, palette: Palette):
        self.p = palette

    def sizeHint(self, option, index) -> QSize:
        return QSize(220, ROW_HEIGHT)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        stat: PageStat | None = index.data(StatRole)

        # -- background ----------------------------------------------------
        body = QRectF(rect.adjusted(6, 3, -6, -3))
        if selected:
            painter.setBrush(QColor(self.p.accent_dim))
            painter.setPen(QPen(QColor(self.p.accent), 1))
        elif hovered:
            painter.setBrush(QColor(self.p.panel_alt))
            painter.setPen(Qt.PenStyle.NoPen)
        else:
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(Qt.PenStyle.NoPen)
        if selected or hovered:
            painter.drawRoundedRect(body, 8, 8)

        if stat is None:
            painter.setPen(QColor(self.p.text_faint))
            painter.drawText(rect.adjusted(14, 0, 0, 0),
                             Qt.AlignmentFlag.AlignVCenter, "…")
            painter.restore()
            return

        x = rect.left() + 14

        # -- thumbnail -----------------------------------------------------
        thumb: QPixmap | None = index.data(ThumbRole)
        thumb_rect = QRect(x, rect.top() + (ROW_HEIGHT - THUMB_H) // 2,
                           THUMB_W, THUMB_H)
        painter.setPen(QPen(QColor(self.p.line), 1))
        painter.setBrush(QColor(self.p.panel_alt))
        painter.drawRoundedRect(QRectF(thumb_rect), 3, 3)
        if thumb is not None and not thumb.isNull():
            scaled = thumb.scaled(THUMB_W - 2, THUMB_H - 2,
                                  Qt.AspectRatioMode.KeepAspectRatio,
                                  Qt.TransformationMode.SmoothTransformation)
            tx = thumb_rect.left() + (THUMB_W - scaled.width()) // 2
            ty = thumb_rect.top() + (THUMB_H - scaled.height()) // 2
            painter.drawPixmap(tx, ty, scaled)

        # A coloured spine makes structural rows scannable at a glance.
        spine = None
        if stat.kind == KIND_ADDED:
            spine = self.p.ok
        elif stat.kind == KIND_REMOVED:
            spine = self.p.danger
        elif stat.status == "error":
            spine = self.p.danger
        elif stat.status == "changed":
            spine = self.p.warn
        if spine:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(spine))
            painter.drawRoundedRect(
                QRectF(rect.left() + 6, rect.top() + 12, 3,
                       ROW_HEIGHT - 24), 1.5, 1.5)

        x += THUMB_W + 12
        text_w = rect.right() - x - 12

        # -- title ---------------------------------------------------------
        font = painter.font()
        font.setPointSizeF(font.pointSizeF() + 0.5)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(self.p.text))
        painter.drawText(QRect(x, rect.top() + 10, text_w, 18),
                         Qt.AlignmentFlag.AlignLeft
                         | Qt.AlignmentFlag.AlignVCenter, stat.label())

        # -- subtitle ------------------------------------------------------
        font.setWeight(QFont.Weight.Normal)
        font.setPointSizeF(font.pointSizeF() - 1.6)
        painter.setFont(font)

        if stat.kind == KIND_ADDED:
            sub, colour = "Added in B", self.p.ok
        elif stat.kind == KIND_REMOVED:
            sub, colour = "Removed from B", self.p.danger
        elif stat.status == "error":
            sub, colour = "Failed to compare", self.p.danger
        elif stat.status == "identical":
            sub, colour = "Identical", self.p.text_faint
        else:
            bits = []
            if stat.text_changes:
                bits.append(f"{stat.text_changes:,} text")
            if stat.image_changes:
                bits.append(f"{stat.image_changes:,} image")
            sub = " · ".join(bits) or "Changed"
            colour = self.p.text_dim
        if stat.flags and stat.status != "identical":
            sub += "  ·  " + stat.flag_labels()[0]
        painter.setPen(QColor(colour))
        painter.drawText(QRect(x, rect.top() + 28, text_w, 15),
                         Qt.AlignmentFlag.AlignLeft
                         | Qt.AlignmentFlag.AlignVCenter, sub)

        # -- change composition bar ---------------------------------------
        if stat.total_changes:
            bar = QRectF(x, rect.top() + 47, text_w, 4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(self.p.line))
            painter.drawRoundedRect(bar, 2, 2)
            total = stat.total_changes
            offset = 0.0
            for cat in list(TEXT_CATEGORIES) + list(IMAGE_CATEGORIES):
                n = stat.counts.get(cat, 0)
                if not n:
                    continue
                w = bar.width() * (n / total)
                painter.setBrush(QColor(CATEGORY_HEX.get(cat, self.p.text_dim)))
                painter.drawRect(QRectF(bar.x() + offset, bar.y(),
                                        max(1.5, w), bar.height()))
                offset += w
        painter.restore()


# --------------------------------------------------------------------------
class PageListView(QListView):
    """List view that reports which rows are visible, for thumbnail loading."""

    visible_changed = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFrameShape(QListView.Shape.NoFrame)

        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(90)
        self._settle.timeout.connect(self._emit_visible)
        self.verticalScrollBar().valueChanged.connect(
            lambda _v: self._settle.start())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._settle.start()

    def _emit_visible(self):
        model = self.model()
        if model is None or model.rowCount() == 0:
            return
        top = self.indexAt(self.viewport().rect().topLeft())
        bottom = self.indexAt(self.viewport().rect().bottomLeft())
        first = top.row() if top.isValid() else 0
        last = bottom.row() if bottom.isValid() else model.rowCount() - 1
        self.visible_changed.emit(max(0, first - 4),
                                  min(model.rowCount() - 1, last + 6))
