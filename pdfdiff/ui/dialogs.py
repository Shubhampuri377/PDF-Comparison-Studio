"""Modal dialogs: licence, comparison options, report export."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout,
                               QFrame, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem,
                               QPlainTextEdit, QProgressBar, QPushButton,
                               QScrollArea, QSpinBox, QTabWidget, QVBoxLayout,
                               QWidget)

from ..ignore import PATTERN_PRESETS
from ..models import CompareSettings, RuntimeSettings
from ..reporting import (ALL_FORMATS, FMT_CSV, FMT_HTML, FMT_JSON, FMT_PDF,
                         ReportOptions)
from .theme import Palette
from .widgets import Card, hline, label, section


# --------------------------------------------------------------------------
class LicenceDialog(QDialog):
    """Enter or review the licence key."""

    def __init__(self, palette: Palette, status=None, parent=None):
        super().__init__(parent)
        self.p = palette
        self.setWindowTitle("Licence")
        self.setMinimumWidth(560)
        self._status = status

        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 24, 26, 22)
        lay.setSpacing(14)

        lay.addWidget(label("Licence", "H2"))

        from ..licensing import short_machine_id

        self.state_label = label("", wrap=True)
        lay.addWidget(self.state_label)

        info = Card(flat=True)
        info_lay = QGridLayout(info)
        info_lay.setContentsMargins(16, 13, 16, 13)
        info_lay.setHorizontalSpacing(16)
        info_lay.setVerticalSpacing(6)
        info_lay.addWidget(label("Machine ID", "Faint"), 0, 0)
        mid = QLineEdit(short_machine_id())
        mid.setReadOnly(True)
        mid.setStyleSheet("font-family:Consolas,monospace;")
        info_lay.addWidget(mid, 0, 1)
        copy_btn = QPushButton("Copy")
        copy_btn.setProperty("flat", True)
        copy_btn.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(short_machine_id()))
        info_lay.addWidget(copy_btn, 0, 2)
        info_lay.addWidget(
            label("Send this to your supplier to obtain a bound licence.",
                  "Faint", wrap=True), 1, 0, 1, 3)
        lay.addWidget(info)

        lay.addWidget(section("Licence key"))
        self.key_edit = QPlainTextEdit()
        self.key_edit.setPlaceholderText("Paste your licence key here…")
        self.key_edit.setFixedHeight(96)
        self.key_edit.setStyleSheet("font-family:Consolas,monospace;"
                                    "font-size:12px;")
        lay.addWidget(self.key_edit)

        self.feedback = label("", wrap=True)
        lay.addWidget(self.feedback)

        buttons = QHBoxLayout()
        self.remove_btn = QPushButton("Remove licence")
        self.remove_btn.setProperty("danger", True)
        self.remove_btn.clicked.connect(self._remove)
        buttons.addWidget(self.remove_btn)
        buttons.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        self.activate_btn = QPushButton("Activate")
        self.activate_btn.setProperty("accent", True)
        self.activate_btn.setDefault(True)
        self.activate_btn.clicked.connect(self._activate)
        buttons.addWidget(close_btn)
        buttons.addWidget(self.activate_btn)
        lay.addLayout(buttons)

        self._refresh()

    def _refresh(self):
        from ..licensing import current_status

        st = self._status or current_status()
        self._status = st
        if st.valid:
            self.state_label.setText(
                f"<b style='color:{self.p.ok}'>Active</b> — licensed to "
                f"<b>{st.licensee}</b>, expires "
                f"{st.expires_at:%d %B %Y} ({st.days_left} days left)."
                + (f"<br><span style='color:{self.p.text_dim}'>"
                   f"Bound to this machine.</span>" if st.machine_bound else ""))
            self.remove_btn.setVisible(True)
        else:
            self.state_label.setText(
                f"<b style='color:{self.p.danger}'>Not licensed</b> — "
                f"{st.reason}.")
            self.remove_btn.setVisible(False)

    def _activate(self):
        from ..licensing import save_licence, verify_licence

        key = self.key_edit.toPlainText().strip()
        if not key:
            self._say("Paste a licence key first.", self.p.warn)
            return
        st = verify_licence(key)
        if not st.valid:
            self._say(st.reason, self.p.danger)
            return
        save_licence(key)
        self._status = st
        self._refresh()
        self._say(f"Activated for {st.licensee}.", self.p.ok)
        QTimer.singleShot(900, self.accept)

    def _remove(self):
        from ..licensing import clear_licence

        clear_licence()
        self._status = None
        self._refresh()
        self._say("Licence removed.", self.p.text_dim)

    def _say(self, text: str, colour: str):
        self.feedback.setText(text)
        self.feedback.setStyleSheet(f"color:{colour};")

    def status(self):
        return self._status


# --------------------------------------------------------------------------
class SettingsDialog(QDialog):
    """Comparison and performance options."""

    def __init__(self, settings: CompareSettings, runtime: RuntimeSettings,
                 palette: Palette, parent=None):
        super().__init__(parent)
        self.p = palette
        self.setWindowTitle("Comparison settings")
        self.setMinimumWidth(600)
        self._settings = CompareSettings.from_dict(settings.to_dict())
        self._runtime = RuntimeSettings.from_dict(runtime.to_dict())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 20)
        outer.setSpacing(14)
        outer.addWidget(label("Comparison settings", "H2"))

        tabs = QTabWidget()
        tabs.addTab(self._text_tab(), "Text")
        tabs.addTab(self._layout_tab(), "Layout && images")
        tabs.addTab(self._patterns_tab(), "Ignore patterns")
        tabs.addTab(self._perf_tab(), "Performance")
        outer.addWidget(tabs)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.Ok).setProperty("accent", True)
        box.button(QDialogButtonBox.StandardButton.RestoreDefaults
                   ).clicked.connect(self._restore)
        outer.addWidget(box)

    # -- tabs --------------------------------------------------------------
    def _text_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(6, 14, 6, 6)
        form.setSpacing(11)

        self.cb_case = QCheckBox("Ignore letter case")
        self.cb_case.setChecked(self._settings.ignore_case)
        self.cb_ws = QCheckBox("Normalise whitespace")
        self.cb_ws.setChecked(self._settings.ignore_whitespace)
        self.cb_punct = QCheckBox("Ignore punctuation")
        self.cb_punct.setChecked(self._settings.ignore_punctuation)
        self.cb_digits = QCheckBox("Ignore digits (compare templates)")
        self.cb_digits.setChecked(self._settings.ignore_digits)
        self.cb_format = QCheckBox("Detect font, size and colour changes")
        self.cb_format.setChecked(self._settings.compare_formatting)
        self.cb_format.setToolTip(
            "Slower: needs full span extraction rather than plain words.")

        for cb in (self.cb_case, self.cb_ws, self.cb_punct, self.cb_digits,
                   self.cb_format):
            form.addRow(cb)

        form.addRow(hline(self.p))
        self.sp_move = QDoubleSpinBox()
        self.sp_move.setRange(0.0, 72.0)
        self.sp_move.setSingleStep(0.5)
        self.sp_move.setSuffix(" pt")
        self.sp_move.setValue(self._settings.move_tolerance_pt)
        form.addRow("Movement tolerance", self.sp_move)
        form.addRow("", label(
            "Text that shifts by less than this is treated as unchanged. "
            "A uniform shift of the whole page is always ignored.",
            "Faint", wrap=True))
        return w

    def _layout_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(6, 14, 6, 6)
        form.setSpacing(11)

        self.sp_header = QDoubleSpinBox()
        self.sp_header.setRange(0.0, 400.0)
        self.sp_header.setSuffix(" pt")
        self.sp_header.setValue(self._settings.header_ignore_pt)
        form.addRow("Ignore header band", self.sp_header)

        self.sp_footer = QDoubleSpinBox()
        self.sp_footer.setRange(0.0, 400.0)
        self.sp_footer.setSuffix(" pt")
        self.sp_footer.setValue(self._settings.footer_ignore_pt)
        form.addRow("Ignore footer band", self.sp_footer)
        form.addRow("", label(
            "72 pt = 1 inch. Footer bands are the usual cure for "
            "page-number and timestamp noise.", "Faint", wrap=True))

        form.addRow(hline(self.p))
        self.cb_images = QCheckBox("Compare embedded images")
        self.cb_images.setChecked(self._settings.compare_images)
        form.addRow(self.cb_images)

        self.cb_fuzzy = QCheckBox("Match re-encoded images (perceptual hash)")
        self.cb_fuzzy.setChecked(self._settings.image_fuzzy)
        form.addRow(self.cb_fuzzy)

        self.sp_ihash = QSpinBox()
        self.sp_ihash.setRange(0, 32)
        self.sp_ihash.setValue(self._settings.image_hash_tolerance)
        form.addRow("Image hash tolerance", self.sp_ihash)

        self.sp_imove = QDoubleSpinBox()
        self.sp_imove.setRange(0.0, 144.0)
        self.sp_imove.setSuffix(" pt")
        self.sp_imove.setValue(self._settings.image_move_tolerance_pt)
        form.addRow("Image movement tolerance", self.sp_imove)

        form.addRow(hline(self.p))
        self.cb_align = QCheckBox("Align pages automatically")
        self.cb_align.setChecked(self._settings.align_pages)
        self.cb_align.setToolTip(
            "Detects inserted and deleted pages so the rest of the document "
            "still lines up. Turn off to compare strictly page 1 to page 1.")
        form.addRow(self.cb_align)

        self.sp_align = QDoubleSpinBox()
        self.sp_align.setRange(0.05, 0.99)
        self.sp_align.setSingleStep(0.05)
        self.sp_align.setValue(self._settings.align_min_similarity)
        form.addRow("Minimum page similarity", self.sp_align)
        return w

    def _patterns_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(6, 14, 6, 6)
        lay.setSpacing(10)

        lay.addWidget(label(
            "Words matching any of these regular expressions are excluded "
            "from the comparison.", "Faint", wrap=True))

        preset_row = QHBoxLayout()
        preset_row.addWidget(label("Add preset:"))
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Choose…", "")
        for name, pattern in PATTERN_PRESETS:
            self.preset_combo.addItem(name, pattern)
        self.preset_combo.currentIndexChanged.connect(self._add_preset)
        preset_row.addWidget(self.preset_combo, 1)
        lay.addLayout(preset_row)

        self.patterns_edit = QPlainTextEdit()
        self.patterns_edit.setPlaceholderText(
            r"One pattern per line, e.g.  ^Page \d+ of \d+$")
        self.patterns_edit.setPlainText("\n".join(self._settings.ignore_patterns))
        self.patterns_edit.setStyleSheet("font-family:Consolas,monospace;")
        lay.addWidget(self.patterns_edit, 1)
        return w

    def _perf_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        form.setContentsMargins(6, 14, 6, 6)
        form.setSpacing(11)

        self.sp_workers = QSpinBox()
        self.sp_workers.setRange(0, 64)
        self.sp_workers.setSpecialValueText("Automatic")
        self.sp_workers.setValue(self._runtime.workers)
        form.addRow("Worker processes", self.sp_workers)
        form.addRow("", label(
            f"Automatic uses {RuntimeSettings().resolved_workers()} on this "
            f"machine.", "Faint"))

        self.sp_chunk = QSpinBox()
        self.sp_chunk.setRange(8, 512)
        self.sp_chunk.setValue(self._runtime.chunk_pages)
        form.addRow("Pages per work unit", self.sp_chunk)

        self.sp_cache = QSpinBox()
        self.sp_cache.setRange(64, 4096)
        self.sp_cache.setSuffix(" MB")
        self.sp_cache.setValue(self._runtime.render_cache_mb)
        form.addRow("Page image cache", self.sp_cache)

        self.sp_boxes = QSpinBox()
        self.sp_boxes.setRange(200, 60000)
        self.sp_boxes.setValue(self._settings.max_boxes_per_page)
        form.addRow("Max highlights per page", self.sp_boxes)
        form.addRow("", label(
            "Caps memory on pathological pages. Counts stay exact; only the "
            "drawn rectangles are limited.", "Faint", wrap=True))
        return w

    # -- behaviour ---------------------------------------------------------
    def _add_preset(self, index: int):
        pattern = self.preset_combo.itemData(index)
        if not pattern:
            return
        current = self.patterns_edit.toPlainText().splitlines()
        if pattern not in current:
            current.append(pattern)
            self.patterns_edit.setPlainText("\n".join(
                [c for c in current if c.strip()]))
        self.preset_combo.setCurrentIndex(0)

    def _restore(self):
        defaults = CompareSettings()
        self.cb_case.setChecked(defaults.ignore_case)
        self.cb_ws.setChecked(defaults.ignore_whitespace)
        self.cb_punct.setChecked(defaults.ignore_punctuation)
        self.cb_digits.setChecked(defaults.ignore_digits)
        self.cb_format.setChecked(defaults.compare_formatting)
        self.sp_move.setValue(defaults.move_tolerance_pt)
        self.sp_header.setValue(defaults.header_ignore_pt)
        self.sp_footer.setValue(defaults.footer_ignore_pt)
        self.cb_images.setChecked(defaults.compare_images)
        self.cb_fuzzy.setChecked(defaults.image_fuzzy)
        self.sp_ihash.setValue(defaults.image_hash_tolerance)
        self.sp_imove.setValue(defaults.image_move_tolerance_pt)
        self.cb_align.setChecked(defaults.align_pages)
        self.sp_align.setValue(defaults.align_min_similarity)
        self.patterns_edit.setPlainText("")
        self.sp_boxes.setValue(defaults.max_boxes_per_page)

    def result_settings(self) -> CompareSettings:
        s = self._settings
        s.ignore_case = self.cb_case.isChecked()
        s.ignore_whitespace = self.cb_ws.isChecked()
        s.ignore_punctuation = self.cb_punct.isChecked()
        s.ignore_digits = self.cb_digits.isChecked()
        s.compare_formatting = self.cb_format.isChecked()
        s.move_tolerance_pt = self.sp_move.value()
        s.header_ignore_pt = self.sp_header.value()
        s.footer_ignore_pt = self.sp_footer.value()
        s.compare_images = self.cb_images.isChecked()
        s.image_fuzzy = self.cb_fuzzy.isChecked()
        s.image_hash_tolerance = self.sp_ihash.value()
        s.image_move_tolerance_pt = self.sp_imove.value()
        s.align_pages = self.cb_align.isChecked()
        s.align_min_similarity = self.sp_align.value()
        s.ignore_patterns = [ln.strip() for ln
                             in self.patterns_edit.toPlainText().splitlines()
                             if ln.strip()]
        s.max_boxes_per_page = self.sp_boxes.value()
        return s

    def result_runtime(self) -> RuntimeSettings:
        r = self._runtime
        r.workers = self.sp_workers.value()
        r.chunk_pages = self.sp_chunk.value()
        r.render_cache_mb = self.sp_cache.value()
        return r


# --------------------------------------------------------------------------
class ReportDialog(QDialog):
    """Choose formats and scope, then write the report."""

    def __init__(self, palette: Palette, default_dir: str, default_name: str,
                 changed_pages: int, parent=None):
        super().__init__(parent)
        self.p = palette
        self.setWindowTitle("Export report")
        self.setMinimumWidth(600)
        self._changed = changed_pages

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(13)
        lay.addWidget(label("Export report", "H2"))

        form = QFormLayout()
        form.setSpacing(10)

        self.title_edit = QLineEdit("PDF Comparison Report")
        form.addRow("Title", self.title_edit)
        self.author_edit = QLineEdit(os.environ.get("USERNAME", ""))
        form.addRow("Prepared by", self.author_edit)

        dir_row = QHBoxLayout()
        self.dir_edit = QLineEdit(default_dir)
        browse = QPushButton("…")
        browse.setFixedWidth(36)
        browse.clicked.connect(self._pick_dir)
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(browse)
        form.addRow("Folder", dir_row)

        self.name_edit = QLineEdit(default_name)
        form.addRow("File name", self.name_edit)
        lay.addLayout(form)

        lay.addWidget(section("Formats"))
        fmt_row = QHBoxLayout()
        self.fmt_boxes = {}
        for fmt, text, default in (
                (FMT_HTML, "HTML (interactive)", True),
                (FMT_PDF, "PDF (printable)", True),
                (FMT_CSV, "CSV (spreadsheet)", False),
                (FMT_JSON, "JSON (pipeline)", False)):
            cb = QCheckBox(text)
            cb.setChecked(default)
            self.fmt_boxes[fmt] = cb
            fmt_row.addWidget(cb)
        fmt_row.addStretch(1)
        lay.addLayout(fmt_row)

        lay.addWidget(section("Contents"))
        self.cb_identical = QCheckBox("Include pages with no differences")
        self.cb_thumbs = QCheckBox("Include page images")
        self.cb_thumbs.setChecked(True)
        self.cb_text = QCheckBox("List the changed words")
        self.cb_text.setChecked(True)
        for cb in (self.cb_thumbs, self.cb_text, self.cb_identical):
            lay.addWidget(cb)

        limit_row = QHBoxLayout()
        limit_row.addWidget(label("Maximum pages to render"))
        self.sp_limit = QSpinBox()
        self.sp_limit.setRange(1, 20000)
        self.sp_limit.setValue(min(250, max(1, changed_pages or 1)))
        limit_row.addWidget(self.sp_limit)
        limit_row.addStretch(1)
        lay.addLayout(limit_row)

        self.hint = label("", "Faint", wrap=True)
        lay.addWidget(self.hint)
        self.sp_limit.valueChanged.connect(self._update_hint)
        self._update_hint()

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        lay.addWidget(self.progress)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.export_btn = QPushButton("Export")
        self.export_btn.setProperty("accent", True)
        self.export_btn.setDefault(True)
        buttons.addButton(self.export_btn,
                          QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _update_hint(self):
        limit = self.sp_limit.value()
        if self._changed > limit:
            self.hint.setText(
                f"{self._changed:,} pages differ. The first {limit:,} will be "
                f"rendered as images; every page still appears in the summary "
                f"table and in the CSV/JSON exports.")
            self.hint.setStyleSheet(f"color:{self.p.warn};")
        else:
            self.hint.setText(
                f"All {self._changed:,} changed pages will be rendered.")
            self.hint.setStyleSheet(f"color:{self.p.text_faint};")

    def _pick_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Choose output folder",
                                                self.dir_edit.text())
        if path:
            self.dir_edit.setText(path)

    def options(self) -> ReportOptions:
        return ReportOptions(
            title=self.title_edit.text().strip() or "PDF Comparison Report",
            author=self.author_edit.text().strip(),
            formats=[f for f, cb in self.fmt_boxes.items() if cb.isChecked()],
            include_identical=self.cb_identical.isChecked(),
            include_thumbnails=self.cb_thumbs.isChecked(),
            include_change_text=self.cb_text.isChecked(),
            max_thumbnails=self.sp_limit.value())

    def output_dir(self) -> str:
        return self.dir_edit.text().strip()

    def basename(self) -> str:
        return self.name_edit.text().strip() or "comparison"

    def set_busy(self, busy: bool):
        self.export_btn.setEnabled(not busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
