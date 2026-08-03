"""Design tokens and the application stylesheet.

One source of truth for colour, spacing and type so the whole app reads as a
single product rather than a pile of widgets.  Both themes are defined here;
the window swaps between them at runtime without a restart.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QIcon, QPainter,
                           QPainterPath, QPen, QPixmap)


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str            # window background
    panel: str         # cards, side rails
    panel_alt: str     # inputs, hover fills
    elevated: str      # popups, headers
    line: str          # borders
    line_soft: str
    text: str
    text_dim: str
    text_faint: str
    accent: str
    accent_hi: str
    accent_dim: str
    on_accent: str
    ok: str
    warn: str
    danger: str
    canvas: str        # the area behind rendered pages
    shadow: str


DARK = Palette(
    name="dark",
    bg="#0e1116", panel="#161b22", panel_alt="#1c2230", elevated="#1f2633",
    line="#252d3a", line_soft="#1e2530",
    text="#e6edf3", text_dim="#8b98a9", text_faint="#5d6b7d",
    accent="#4f8cff", accent_hi="#6ba0ff", accent_dim="#2c4f8f",
    on_accent="#ffffff",
    ok="#22b26e", warn="#f09e1e", danger="#e83e4a",
    canvas="#0a0d12", shadow="rgba(0,0,0,0.45)",
)

LIGHT = Palette(
    name="light",
    bg="#f4f6fa", panel="#ffffff", panel_alt="#f1f4f9", elevated="#ffffff",
    line="#e2e7ef", line_soft="#eef1f6",
    text="#111827", text_dim="#5b6676", text_faint="#8b95a3",
    accent="#2f6fe4", accent_hi="#1f5ed0", accent_dim="#c9dcff",
    on_accent="#ffffff",
    ok="#128a52", warn="#b96f00", danger="#c62630",
    canvas="#e7ebf1", shadow="rgba(15,23,42,0.12)",
)

# Category colours mirror pdfdiff.render.HIGHLIGHT_COLOURS so the viewer, the
# legend and the exported reports agree.
CATEGORY_HEX = {
    "removed": "#e83e4a",
    "added": "#22b26e",
    "changed": "#f09e1e",
    "moved": "#5680e9",
    "format": "#a75fe0",
    "img_removed": "#e83e4a",
    "img_added": "#22b26e",
    "img_moved": "#5680e9",
    "img_changed": "#f09e1e",
}

RADIUS = 10
RADIUS_SM = 7
RADIUS_LG = 14


def ui_font(size: int = 10, weight: int = QFont.Weight.Normal) -> QFont:
    for family in ("Inter", "Segoe UI Variable Text", "Segoe UI"):
        if family in QFontDatabase.families():
            f = QFont(family, size)
            f.setWeight(weight)
            return f
    f = QFont()
    f.setPointSize(size)
    f.setWeight(weight)
    return f


def mono_font(size: int = 9) -> QFont:
    for family in ("Cascadia Code", "Cascadia Mono", "Consolas"):
        if family in QFontDatabase.families():
            return QFont(family, size)
    f = QFont()
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(size)
    return f


def sun_icon(colour: str, size: int = 16) -> QIcon:
    """Sun glyph drawn with QPainter.

    Drawn rather than typed: the ``☀`` and ``◐`` characters fall back to
    whatever font happens to have them and render as a barely visible dot on
    Windows, which is what made the original theme toggle impossible to find.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(colour)
    p.setPen(QPen(c, max(1.3, size * 0.09), Qt.PenStyle.SolidLine,
                  Qt.PenCapStyle.RoundCap))
    p.setBrush(Qt.BrushStyle.NoBrush)

    centre = size / 2.0
    r = size * 0.20
    p.drawEllipse(QPointF(centre, centre), r, r)
    for i in range(8):
        angle = math.radians(i * 45)
        dx, dy = math.cos(angle), math.sin(angle)
        p.drawLine(QPointF(centre + dx * r * 1.7, centre + dy * r * 1.7),
                   QPointF(centre + dx * r * 2.35, centre + dy * r * 2.35))
    p.end()
    return QIcon(pm)


def moon_icon(colour: str, size: int = 16) -> QIcon:
    """Crescent moon, drawn as one filled circle minus an offset circle."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)

    path = QPainterPath()
    centre = size / 2.0
    r = size * 0.36
    path.addEllipse(QPointF(centre, centre), r, r)
    bite = QPainterPath()
    bite.addEllipse(QPointF(centre + r * 0.55, centre - r * 0.42), r * 0.92,
                    r * 0.92)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(colour))
    p.drawPath(path.subtracted(bite))
    p.end()
    return QIcon(pm)


def rect_select_icon(colour: str, size: int = 16) -> QIcon:
    """Marquee-selection glyph: a dashed rectangle with corner handles."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    c = QColor(colour)

    pen = QPen(c, 1.3, Qt.PenStyle.DashLine)
    pen.setDashPattern([2.2, 1.8])
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    inset = size * 0.17
    p.drawRect(QRectF(inset, inset, size - 2 * inset, size - 2 * inset))

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(c)
    h = max(1.7, size * 0.11)
    for cx, cy in ((inset, inset), (size - inset, inset),
                   (inset, size - inset), (size - inset, size - inset)):
        p.drawRect(QRectF(cx - h, cy - h, h * 2, h * 2))
    p.end()
    return QIcon(pm)


def swatch(hex_colour: str, size: int = 12, radius: int = 3) -> QIcon:
    """Small rounded colour chip used in legends and combo boxes."""
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(hex_colour))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(0, 0, size, size, radius, radius)
    p.end()
    return QIcon(pm)


def qpalette(p: Palette):
    """Build a real ``QPalette`` to sit underneath the stylesheet.

    Item views resolve text colour from the widget palette, not from QSS, so a
    stylesheet-only theme swap leaves ``QListWidget`` rows painted in the old
    theme's colour -- white on white after switching to light.
    """
    from PySide6.QtGui import QPalette

    pal = QPalette()
    text = QColor(p.text)
    dim = QColor(p.text_dim)
    base = QColor(p.panel)
    alt = QColor(p.panel_alt)

    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive):
        pal.setColor(group, QPalette.ColorRole.Window, QColor(p.bg))
        pal.setColor(group, QPalette.ColorRole.WindowText, text)
        pal.setColor(group, QPalette.ColorRole.Base, base)
        pal.setColor(group, QPalette.ColorRole.AlternateBase, alt)
        pal.setColor(group, QPalette.ColorRole.Text, text)
        pal.setColor(group, QPalette.ColorRole.Button, alt)
        pal.setColor(group, QPalette.ColorRole.ButtonText, text)
        pal.setColor(group, QPalette.ColorRole.ToolTipBase, QColor(p.elevated))
        pal.setColor(group, QPalette.ColorRole.ToolTipText, text)
        pal.setColor(group, QPalette.ColorRole.PlaceholderText, dim)
        pal.setColor(group, QPalette.ColorRole.Highlight, QColor(p.accent))
        pal.setColor(group, QPalette.ColorRole.HighlightedText,
                     QColor(p.on_accent))
        pal.setColor(group, QPalette.ColorRole.Link, QColor(p.accent))

    disabled = QPalette.ColorGroup.Disabled
    faint = QColor(p.text_faint)
    pal.setColor(disabled, QPalette.ColorRole.Text, faint)
    pal.setColor(disabled, QPalette.ColorRole.WindowText, faint)
    pal.setColor(disabled, QPalette.ColorRole.ButtonText, faint)
    return pal


def stylesheet(p: Palette) -> str:
    """The whole application stylesheet, generated from a palette."""
    return f"""
* {{
    outline: none;
}}
QWidget {{
    background: transparent;
    color: {p.text};
    font-size: 13px;
}}
QMainWindow, QDialog {{
    background: {p.bg};
}}

/* ---------- surfaces ---------- */
#Card, #Panel {{
    background: {p.panel};
    border: 1px solid {p.line};
    border-radius: {RADIUS_LG}px;
}}
#FlatCard {{
    background: {p.panel};
    border: 1px solid {p.line};
    border-radius: {RADIUS}px;
}}
#Rail {{
    background: {p.panel};
    border-right: 1px solid {p.line};
}}
#RailRight {{
    background: {p.panel};
    border-left: 1px solid {p.line};
}}
#TopBar {{
    background: {p.elevated};
    border-bottom: 1px solid {p.line};
}}
#StatusBar {{
    background: {p.panel};
    border-top: 1px solid {p.line};
}}
#Canvas {{
    background: {p.canvas};
    border: none;
}}

/* ---------- type ---------- */
#H1 {{ font-size: 30px; font-weight: 650; letter-spacing: -0.5px; }}
#H2 {{ font-size: 19px; font-weight: 620; letter-spacing: -0.2px; }}
#H3 {{ font-size: 14px; font-weight: 620; }}
#Muted {{ color: {p.text_dim}; }}
#Faint {{ color: {p.text_faint}; font-size: 12px; }}
#SectionLabel {{
    color: {p.text_dim};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.1px;
}}
#StatValue {{ font-size: 24px; font-weight: 640; letter-spacing: -0.5px; }}
#StatKey {{
    color: {p.text_dim}; font-size: 10px;
    font-weight: 650; letter-spacing: 0.9px;
}}

/* ---------- buttons ---------- */
QPushButton {{
    background: {p.panel_alt};
    color: {p.text};
    border: 1px solid {p.line};
    border-radius: {RADIUS_SM}px;
    padding: 7px 14px;
    font-weight: 550;
}}
QPushButton:hover  {{ background: {p.elevated}; border-color: {p.accent_dim}; }}
QPushButton:pressed{{ background: {p.line}; }}
QPushButton:disabled {{ color: {p.text_faint}; background: {p.panel};
                        border-color: {p.line_soft}; }}
QPushButton[accent="true"] {{
    background: {p.accent};
    color: {p.on_accent};
    border: 1px solid {p.accent};
    font-weight: 600;
}}
QPushButton[accent="true"]:hover   {{ background: {p.accent_hi};
                                      border-color: {p.accent_hi}; }}
QPushButton[accent="true"]:pressed {{ background: {p.accent_dim}; }}
QPushButton[accent="true"]:disabled{{ background: {p.panel_alt};
                                      color: {p.text_faint};
                                      border-color: {p.line}; }}
QPushButton[danger="true"] {{ color: {p.danger}; }}
QPushButton[danger="true"]:hover {{ background: rgba(232,62,74,0.12);
                                    border-color: {p.danger}; }}
QPushButton[flat="true"] {{
    background: transparent; border: 1px solid transparent; padding: 6px 10px;
}}
QPushButton[flat="true"]:hover {{ background: {p.panel_alt};
                                  border-color: {p.line}; }}
QPushButton[flat="true"]:checked {{
    background: {p.accent_dim}; border-color: {p.accent};
    color: {p.text};
}}
QPushButton[chip="true"] {{
    border-radius: 999px; padding: 5px 13px; font-size: 12px;
}}
QPushButton[chip="true"]:checked {{
    background: {p.accent}; color: {p.on_accent}; border-color: {p.accent};
}}
QToolButton {{
    background: transparent; border: 1px solid transparent;
    border-radius: {RADIUS_SM}px; padding: 5px;
}}
QToolButton:hover   {{ background: {p.panel_alt}; border-color: {p.line}; }}
QToolButton:checked {{ background: {p.accent_dim}; border-color: {p.accent}; }}

/* ---------- inputs ---------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{
    background: {p.panel_alt};
    border: 1px solid {p.line};
    border-radius: {RADIUS_SM}px;
    padding: 6px 10px;
    selection-background-color: {p.accent};
    selection-color: {p.on_accent};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus,
QPlainTextEdit:focus, QTextEdit:focus {{
    border-color: {p.accent};
}}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{
    color: {p.text_faint};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox::down-arrow {{
    /* Pure-border triangle. width/height must be zero or the box model
       leaves a visible square behind the borders. */
    image: none;
    width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.text_dim};
    border-bottom: 0;
    margin-right: 9px;
}}
QComboBox::down-arrow:on {{ border-top-color: {p.accent}; }}
QComboBox QAbstractItemView {{
    background: {p.elevated};
    border: 1px solid {p.line};
    border-radius: {RADIUS_SM}px;
    padding: 4px;
    selection-background-color: {p.accent};
    selection-color: {p.on_accent};
    outline: none;
}}
QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{ width: 0; border: none; }}

QCheckBox, QRadioButton {{ spacing: 8px; padding: 2px 0; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {p.line}; background: {p.panel_alt};
}}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p.accent};
}}
QCheckBox::indicator:checked {{
    background: {p.accent}; border-color: {p.accent};
    image: none;
}}
QRadioButton::indicator:checked {{
    background: {p.accent}; border: 4px solid {p.panel_alt};
}}

/* ---------- lists / tables ---------- */
QListView, QTreeView, QTableView {{
    background: {p.panel};
    /* Set colour explicitly. Without it the item delegate resolves text from
       a palette captured at polish time, which survives a theme swap and
       leaves rows painted white-on-white after switching to light. */
    color: {p.text};
    border: 1px solid {p.line};
    border-radius: {RADIUS}px;
    outline: none;
    selection-background-color: transparent;
}}
QListView::item, QTreeView::item {{
    border: none;
    color: {p.text};
}}
QListView::item:selected, QTreeView::item:selected {{
    color: {p.text};
    background: {p.accent_dim};
}}
QListView::item:hover, QTreeView::item:hover {{ background: {p.panel_alt}; }}
QHeaderView::section {{
    background: {p.panel_alt};
    color: {p.text_dim};
    border: none;
    border-bottom: 1px solid {p.line};
    padding: 7px 10px;
    font-size: 11px; font-weight: 650;
}}

/* ---------- scrollbars ---------- */
QScrollBar:vertical {{
    background: transparent; width: 11px; margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {p.line}; border-radius: 5px; min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.text_faint}; }}
QScrollBar:horizontal {{
    background: transparent; height: 11px; margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {p.line}; border-radius: 5px; min-width: 32px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.text_faint}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- progress ---------- */
QProgressBar {{
    background: {p.panel_alt};
    border: none; border-radius: 4px;
    height: 7px; text-align: center; color: transparent;
}}
QProgressBar::chunk {{ background: {p.accent}; border-radius: 4px; }}

/* ---------- tabs ---------- */
QTabWidget::pane {{
    border: none;
    border-top: 1px solid {p.line};
    top: -1px;
}}
QTabBar {{ qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: transparent;
    /* Unselected tabs must stay legible. Left unstyled they inherit a dim
       palette role and read as disabled rather than as available. */
    color: {p.text_dim};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 8px 15px;
    margin-right: 2px;
    font-weight: 550;
}}
QTabBar::tab:hover {{
    color: {p.text};
    background: {p.panel_alt};
    border-top-left-radius: {RADIUS_SM}px;
    border-top-right-radius: {RADIUS_SM}px;
}}
QTabBar::tab:selected {{
    color: {p.text};
    border-bottom: 2px solid {p.accent};
    font-weight: 650;
}}
QTabBar::tab:disabled {{ color: {p.text_faint}; }}

/* ---------- misc ---------- */
QSplitter::handle {{ background: {p.line}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QToolTip {{
    background: {p.elevated};
    color: {p.text};
    border: 1px solid {p.line};
    border-radius: 6px;
    padding: 5px 8px;
}}
QMenu {{
    background: {p.elevated};
    border: 1px solid {p.line};
    border-radius: {RADIUS_SM}px;
    padding: 5px;
}}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; }}
QMenu::item:selected {{ background: {p.accent}; color: {p.on_accent}; }}
QMenu::separator {{ height: 1px; background: {p.line}; margin: 4px 8px; }}
#Separator {{ background: {p.line}; }}
QGraphicsView {{ border: none; background: {p.canvas}; }}
"""
