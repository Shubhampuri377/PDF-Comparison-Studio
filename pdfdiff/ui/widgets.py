"""Reusable UI components.

Widgets that paint themselves cannot pick up a theme change from the
stylesheet, because they hold a :class:`Palette` rather than reading QSS.  Each
such widget implements ``apply_palette``; :func:`propagate_palette` walks the
object tree and calls it, which is what makes the light/dark toggle repaint the
page list, the viewer canvas and the drop zones rather than just the chrome.
"""
from __future__ import annotations

import os

from PySide6.QtCore import (QEasingCurve, QEvent, QPoint,
                            QPropertyAnimation, QRectF, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter,
                           QPainterPath, QPen)
from PySide6.QtWidgets import (QCheckBox, QFrame, QGraphicsDropShadowEffect,
                               QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from .theme import CATEGORY_HEX, Palette, RADIUS, RADIUS_LG


# --------------------------------------------------------------------------
def propagate_palette(root, palette: Palette):
    """Push a new palette through every self-painting widget under ``root``."""
    from PySide6.QtCore import QObject

    if hasattr(root, "apply_palette"):
        root.apply_palette(palette)
    for child in root.findChildren(QObject):
        fn = getattr(child, "apply_palette", None)
        if callable(fn):
            fn(palette)


# --------------------------------------------------------------------------
def hline(palette: Palette) -> QFrame:
    f = QFrame()
    f.setObjectName("Separator")
    f.setFixedHeight(1)
    return f


def label(text: str, object_name: str = "", wrap: bool = False) -> QLabel:
    lb = QLabel(text)
    if object_name:
        lb.setObjectName(object_name)
    lb.setWordWrap(wrap)
    return lb


def section(text: str) -> QLabel:
    return label(text.upper(), "SectionLabel")


# --------------------------------------------------------------------------
class Card(QFrame):
    def __init__(self, parent=None, flat: bool = False):
        super().__init__(parent)
        self.setObjectName("FlatCard" if flat else "Card")


class StatTile(QFrame):
    """Compact key/value tile used across the summary bar."""

    clicked = Signal()

    def __init__(self, key: str, value: str = "—", accent: str | None = None,
                 clickable: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("FlatCard")
        self._clickable = clickable
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 11)
        lay.setSpacing(2)
        self.key_label = label(key.upper(), "StatKey")
        self.value_label = label(value, "StatValue")
        if accent:
            self.value_label.setStyleSheet(f"color:{accent};")
        lay.addWidget(self.key_label)
        lay.addWidget(self.value_label)
        self._accent = accent

    def set_value(self, text: str):
        self.value_label.setText(text)

    def set_accent(self, colour: str | None):
        self._accent = colour
        self.value_label.setStyleSheet(f"color:{colour};" if colour else "")

    def mouseReleaseEvent(self, event):
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class Pill(QLabel):
    """Small status badge."""

    def __init__(self, text: str = "", colour: str = "#8b98a9", parent=None):
        super().__init__(text, parent)
        self.set_colour(colour)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # A QLabel defaults to a vertically expanding policy, which stretches
        # the rounded background to the full height of a toolbar.
        self.setSizePolicy(QSizePolicy.Policy.Maximum,
                           QSizePolicy.Policy.Fixed)
        self.setFixedHeight(24)

    def set_colour(self, colour: str):
        c = QColor(colour)
        self.setStyleSheet(
            f"background: rgba({c.red()},{c.green()},{c.blue()},40);"
            f"color:{colour}; border-radius:9px; padding:2px 9px;"
            f"font-size:11px; font-weight:650;")

    def set_state(self, text: str, colour: str):
        self.setText(text)
        self.set_colour(colour)


class LegendChip(QWidget):
    """Colour swatch + label + count, toggles a highlight layer."""

    toggled = Signal(str, bool)

    def __init__(self, category: str, text: str, parent=None):
        super().__init__(parent)
        self.category = category
        self._colour = CATEGORY_HEX.get(category, "#8b98a9")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(7)

        self.check = QCheckBox()
        self.check.setChecked(True)
        self.check.setStyleSheet(
            f"QCheckBox::indicator:checked{{background:{self._colour};"
            f"border-color:{self._colour};}}")
        self.check.toggled.connect(
            lambda on: self.toggled.emit(self.category, on))

        self.text_label = label(text)
        self.count_label = label("0", "Faint")

        lay.addWidget(self.check)
        lay.addWidget(self.text_label)
        lay.addStretch(1)
        lay.addWidget(self.count_label)

    def set_count(self, n: int):
        self.count_label.setText(f"{n:,}")
        self.setEnabled(True)
        if n == 0:
            self.count_label.setText("—")

    def is_on(self) -> bool:
        return self.check.isChecked()


# --------------------------------------------------------------------------
class DropZone(QFrame):
    """File picker that also accepts a dragged PDF."""

    file_chosen = Signal(str)

    def __init__(self, tag: str, hint: str, palette: Palette, parent=None):
        super().__init__(parent)
        self.setObjectName("Card")
        self.setAcceptDrops(True)
        self._palette = palette
        self._path = ""
        self._hover = False
        self.setMinimumHeight(148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 20)
        lay.setSpacing(5)

        self.tag_label = label(tag.upper(), "SectionLabel")
        self.tag_label.setStyleSheet(f"color:{palette.accent};")
        self.name_label = label("Drop a PDF here", "H3", wrap=True)
        self.meta_label = label(hint, "Faint", wrap=True)

        lay.addWidget(self.tag_label)
        lay.addSpacing(2)
        lay.addWidget(self.name_label)
        lay.addWidget(self.meta_label)
        lay.addStretch(1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setProperty("flat", True)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setProperty("flat", True)
        self.clear_btn.setVisible(False)
        row.addWidget(self.browse_btn)
        row.addWidget(self.clear_btn)
        row.addStretch(1)
        lay.addLayout(row)

        self._meta_danger = False
        self.browse_btn.clicked.connect(self._browse)
        self.clear_btn.clicked.connect(lambda: self.set_path(""))

    # -- state -------------------------------------------------------------
    def path(self) -> str:
        return self._path

    def set_path(self, path: str):
        self._path = path
        if path:
            self.name_label.setText(os.path.basename(path))
            self.meta_label.setText("Reading…")
            self.clear_btn.setVisible(True)
        else:
            self.name_label.setText("Drop a PDF here")
            self.meta_label.setText("or click Browse")
            self.clear_btn.setVisible(False)
        self.file_chosen.emit(path)
        self.update()

    def set_meta(self, text: str, danger: bool = False):
        self._meta_danger = danger
        self.meta_label.setText(text)
        colour = self._palette.danger if danger else self._palette.text_faint
        self.meta_label.setStyleSheet(f"color:{colour};")

    def apply_palette(self, palette: Palette):
        self._palette = palette
        self.tag_label.setStyleSheet(f"color:{palette.accent};")
        colour = (palette.danger if getattr(self, "_meta_danger", False)
                  else palette.text_faint)
        self.meta_label.setStyleSheet(f"color:{colour};")
        self.update()

    # -- interaction -------------------------------------------------------
    def _browse(self):
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Select PDF", os.path.dirname(self._path) or "",
            "PDF documents (*.pdf)")
        if path:
            self.set_path(path)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and not self._path:
            self._browse()
        super().mouseReleaseEvent(event)

    def dragEnterEvent(self, event):
        if self._urls(event):
            self._hover = True
            self.update()
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._hover = False
        self.update()

    def dropEvent(self, event):
        urls = self._urls(event)
        self._hover = False
        self.update()
        if urls:
            self.set_path(urls[0])
            event.acceptProposedAction()

    @staticmethod
    def _urls(event) -> list[str]:
        md = event.mimeData()
        if not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls()
                if u.toLocalFile().lower().endswith(".pdf")]

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._hover and self._path:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colour = QColor(self._palette.accent if self._hover
                        else self._palette.line)
        pen = QPen(colour, 1.6 if self._hover else 1.2)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 4])
        p.setPen(pen)
        r = self.rect().adjusted(7, 7, -8, -8)
        p.drawRoundedRect(QRectF(r), RADIUS_LG - 3, RADIUS_LG - 3)
        p.end()


# --------------------------------------------------------------------------
class Spinner(QWidget):
    """Indeterminate activity indicator."""

    def __init__(self, palette: Palette, size: int = 18, parent=None):
        super().__init__(parent)
        self._palette = palette
        self._angle = 0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self.hide()

    def apply_palette(self, palette: Palette):
        self._palette = palette
        self.update()

    def start(self):
        self.show()
        if not self._timer.isActive():
            self._timer.start(45)

    def stop(self):
        self._timer.stop()
        self.hide()

    def _tick(self):
        self._angle = (self._angle + 30) % 360
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        side = min(self.width(), self.height())
        p.translate(self.width() / 2, self.height() / 2)
        p.rotate(self._angle)
        base = QColor(self._palette.accent)
        for i in range(8):
            base.setAlphaF(0.18 + 0.82 * (i / 8.0))
            p.setPen(QPen(base, max(1.6, side * 0.11),
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.drawLine(0, int(side * 0.24), 0, int(side * 0.42))
            p.rotate(45)
        p.end()


class Toast(QFrame):
    """Transient message anchored to the bottom of its parent."""

    def __init__(self, parent, palette: Palette):
        super().__init__(parent)
        self._palette = palette
        self.setObjectName("FlatCard")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 11, 16, 11)
        self._label = QLabel("")
        self._label.setWordWrap(True)
        lay.addWidget(self._label)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 120))
        self.setGraphicsEffect(shadow)

        self._anim = QPropertyAnimation(self, b"windowOpacity", self)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._fade_out)
        self.hide()

    def apply_palette(self, palette: Palette):
        self._palette = palette

    def show_message(self, text: str, kind: str = "info", msecs: int = 3200):
        colours = {"info": self._palette.accent, "ok": self._palette.ok,
                   "warn": self._palette.warn, "error": self._palette.danger}
        colour = colours.get(kind, self._palette.accent)
        self._label.setText(text)
        self.setStyleSheet(
            f"#FlatCard{{background:{self._palette.elevated};"
            f"border:1px solid {colour}; border-radius:{RADIUS}px;}}")
        self.adjustSize()
        self._reposition()
        self.setWindowOpacity(1.0)
        self.show()
        self.raise_()
        self._timer.start(msecs)

    def _reposition(self):
        if not self.parent():
            return
        pw = self.parent().width()
        ph = self.parent().height()
        self.setFixedWidth(min(520, max(260, pw - 80)))
        self.adjustSize()
        self.move((pw - self.width()) // 2, ph - self.height() - 34)

    def _fade_out(self):
        self._anim.stop()
        self._anim.setDuration(280)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        self._anim.finished.connect(self.hide)
        self._anim.start()


# --------------------------------------------------------------------------
class SegmentedControl(QWidget):
    """Mutually exclusive chip row."""

    changed = Signal(str)

    # Horizontal padding declared for [chip="true"] in the stylesheet.
    _CHIP_PADDING = 13

    def __init__(self, options: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        self._buttons: dict[str, QPushButton] = {}
        self._labels: dict[str, str] = {}
        for key, text in options:
            btn = QPushButton(text)
            btn.setCheckable(True)
            btn.setProperty("chip", True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, k=key: self.set_current(k))
            lay.addWidget(btn)
            self._buttons[key] = btn
            self._labels[key] = text
        lay.addStretch(1)
        self._resize_chips()
        if options:
            self.set_current(options[0][0], emit=False)

    def _resize_chips(self):
        """Size each chip from its own text.

        Qt does not reliably fold stylesheet padding into a QPushButton's
        size hint, so with a wider system font the label gets clipped at both
        ends -- "Side by side" renders as "ide by sid". Measuring the string
        and setting an explicit minimum makes the chip fit whatever font the
        machine actually uses.
        """
        for key, btn in self._buttons.items():
            metrics = QFontMetrics(btn.font())
            text_w = metrics.horizontalAdvance(self._labels.get(key, btn.text()))
            extra = btn.iconSize().width() + 6 if not btn.icon().isNull() else 0
            btn.setMinimumWidth(text_w + extra + self._CHIP_PADDING * 2 + 8)
            btn.setMinimumHeight(metrics.height() + 12)

    def changeEvent(self, event):
        # The application font changes when the theme is applied.
        super().changeEvent(event)
        if event.type() == QEvent.Type.FontChange:
            self._resize_chips()

    def apply_palette(self, _palette):
        self._resize_chips()

    def set_current(self, key: str, emit: bool = True):
        for k, btn in self._buttons.items():
            btn.setChecked(k == key)
        self._current = key
        if emit:
            self.changed.emit(key)

    def current(self) -> str:
        return getattr(self, "_current", "")

    def set_badge(self, key: str, count: int | None, base: str):
        btn = self._buttons.get(key)
        if btn:
            btn.setText(base if count is None else f"{base}  {count:,}")


class SimilarityBar(QWidget):
    """Thin horizontal meter used in the page list delegate and inspector."""

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self._palette = palette
        self._value = 1.0
        self.setFixedHeight(6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Fixed)

    def apply_palette(self, palette: Palette):
        self._palette = palette
        self.update()

    def set_value(self, value: float):
        self._value = max(0.0, min(1.0, value))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(self._palette.line))
        p.drawRoundedRect(r, 3, 3)
        if self._value > 0:
            if self._value > 0.95:
                colour = self._palette.ok
            elif self._value > 0.6:
                colour = self._palette.warn
            else:
                colour = self._palette.danger
            p.setBrush(QColor(colour))
            p.drawRoundedRect(QRectF(r.x(), r.y(), r.width() * self._value,
                                     r.height()), 3, 3)
        p.end()
