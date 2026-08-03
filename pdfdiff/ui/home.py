"""Home (document selection) and progress screens."""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                               QProgressBar, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

from ..engine import (PHASE_ALIGN, PHASE_DIFF, PHASE_DONE, PHASE_INGEST,
                      PHASE_LABELS, PHASE_PROBE)
from ..models import CompareSettings, Progress
from .theme import Palette
from .widgets import (Card, DropZone, Spinner, StatTile, hline, label, section)


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


class HomePage(QWidget):
    """Pick two PDFs, set the headline options, start."""

    compare_requested = Signal()
    settings_requested = Signal()
    files_changed = Signal()

    def __init__(self, palette: Palette, settings: CompareSettings, parent=None):
        super().__init__(parent)
        self.p = palette
        self.settings = settings
        self._probes: dict[int, dict] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        centre = QWidget()
        centre.setMaximumWidth(1080)
        lay = QVBoxLayout(centre)
        lay.setContentsMargins(40, 34, 40, 34)
        lay.setSpacing(20)

        # -- heading -------------------------------------------------------
        head = QVBoxLayout()
        head.setSpacing(5)
        head.addWidget(label("Compare two PDFs", "H1"))
        head.addWidget(label(
            "Word-level differences, image changes and page insertions — "
            "built to stay responsive on documents of any size.", "Muted",
            wrap=True))
        lay.addLayout(head)

        # -- drop zones ----------------------------------------------------
        zones = QHBoxLayout()
        zones.setSpacing(16)
        self.zone_a = DropZone("Document A — original", "or click Browse",
                               palette)
        self.zone_b = DropZone("Document B — revised", "or click Browse",
                               palette)
        zones.addWidget(self.zone_a, 1)
        zones.addWidget(self.zone_b, 1)
        lay.addLayout(zones)

        self.swap_btn = QPushButton("⇄  Swap documents")
        self.swap_btn.setProperty("flat", True)
        self.swap_btn.clicked.connect(self._swap)
        swap_row = QHBoxLayout()
        swap_row.addStretch(1)
        swap_row.addWidget(self.swap_btn)
        swap_row.addStretch(1)
        lay.addLayout(swap_row)

        # -- quick options -------------------------------------------------
        opts = Card()
        opts_lay = QVBoxLayout(opts)
        opts_lay.setContentsMargins(20, 16, 20, 18)
        opts_lay.setSpacing(11)

        header = QHBoxLayout()
        header.addWidget(section("Comparison options"))
        header.addStretch(1)
        more = QPushButton("All settings…")
        more.setProperty("flat", True)
        more.clicked.connect(self.settings_requested.emit)
        header.addWidget(more)
        opts_lay.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(28)
        grid.setVerticalSpacing(8)
        self.cb_images = QCheckBox("Compare embedded images")
        self.cb_images.setChecked(settings.compare_images)
        self.cb_align = QCheckBox("Detect inserted and deleted pages")
        self.cb_align.setChecked(settings.align_pages)
        self.cb_case = QCheckBox("Ignore letter case")
        self.cb_case.setChecked(settings.ignore_case)
        self.cb_format = QCheckBox("Detect formatting changes")
        self.cb_format.setChecked(settings.compare_formatting)
        grid.addWidget(self.cb_align, 0, 0)
        grid.addWidget(self.cb_images, 0, 1)
        grid.addWidget(self.cb_case, 1, 0)
        grid.addWidget(self.cb_format, 1, 1)
        opts_lay.addLayout(grid)
        lay.addWidget(opts)

        # -- action --------------------------------------------------------
        action = QHBoxLayout()
        action.setSpacing(12)
        self.hint = label("Select two PDF documents to begin.", "Faint",
                          wrap=True)
        action.addWidget(self.hint, 1)
        self.compare_btn = QPushButton("Compare  →")
        self.compare_btn.setProperty("accent", True)
        self.compare_btn.setMinimumHeight(42)
        self.compare_btn.setMinimumWidth(168)
        self.compare_btn.setEnabled(False)
        self.compare_btn.clicked.connect(self.compare_requested.emit)
        action.addWidget(self.compare_btn)
        lay.addLayout(action)

        wrapper = QHBoxLayout()
        wrapper.addStretch(1)
        wrapper.addWidget(centre)
        wrapper.addStretch(1)
        # Sit the panel a little above centre rather than pinned to the top --
        # on a maximised 1440p window a top-anchored form looks abandoned.
        root.addStretch(2)
        root.addLayout(wrapper)
        root.addStretch(3)

        self.zone_a.file_chosen.connect(lambda p: self._on_file(0, p))
        self.zone_b.file_chosen.connect(lambda p: self._on_file(1, p))
        for cb in (self.cb_images, self.cb_align, self.cb_case, self.cb_format):
            cb.toggled.connect(self._sync_settings)

    def apply_palette(self, palette: Palette):
        self.p = palette

    # -- state -------------------------------------------------------------
    def paths(self) -> tuple[str, str]:
        return self.zone_a.path(), self.zone_b.path()

    def _swap(self):
        a, b = self.zone_a.path(), self.zone_b.path()
        self.zone_a.set_path(b)
        self.zone_b.set_path(a)

    def _sync_settings(self):
        self.settings.compare_images = self.cb_images.isChecked()
        self.settings.align_pages = self.cb_align.isChecked()
        self.settings.ignore_case = self.cb_case.isChecked()
        self.settings.compare_formatting = self.cb_format.isChecked()

    def refresh_from_settings(self):
        self.cb_images.setChecked(self.settings.compare_images)
        self.cb_align.setChecked(self.settings.align_pages)
        self.cb_case.setChecked(self.settings.ignore_case)
        self.cb_format.setChecked(self.settings.compare_formatting)

    def _on_file(self, slot: int, path: str):
        self._probes.pop(slot, None)
        self._update_ready()
        self.files_changed.emit()

    def set_probe(self, slot: int, probe: dict):
        self._probes[slot] = probe
        zone = self.zone_a if slot == 0 else self.zone_b
        if probe.get("error"):
            zone.set_meta(probe["error"], danger=True)
        else:
            bits = [f"{probe.get('page_count', 0):,} pages",
                    _human_size(probe.get("file_size", 0))]
            if probe.get("title"):
                bits.append(probe["title"][:44])
            zone.set_meta("  ·  ".join(bits))
        self._update_ready()

    def _update_ready(self):
        a, b = self.paths()
        if not a or not b:
            self.hint.setText("Select two PDF documents to begin.")
            self.compare_btn.setEnabled(False)
            return
        if os.path.abspath(a) == os.path.abspath(b):
            self.hint.setText("Both slots point at the same file.")
            self.compare_btn.setEnabled(False)
            return
        pa, pb = self._probes.get(0), self._probes.get(1)
        if pa is None or pb is None:
            self.hint.setText("Reading documents…")
            self.compare_btn.setEnabled(False)
            return
        if pa.get("error") or pb.get("error"):
            self.hint.setText("Fix the problem above before comparing.")
            self.compare_btn.setEnabled(False)
            return

        n_a, n_b = pa.get("page_count", 0), pb.get("page_count", 0)
        total = n_a + n_b
        if n_a == n_b:
            msg = f"{n_a:,} pages each."
        else:
            msg = f"{n_a:,} vs {n_b:,} pages — page alignment will map them."
        if total > 4000:
            msg += "  Large run: extraction is parallelised and results stream to disk."
        self.hint.setText(msg)
        self.compare_btn.setEnabled(True)


# --------------------------------------------------------------------------
class ProgressPage(QWidget):
    """Phase-by-phase progress with throughput and a working cancel."""

    cancel_requested = Signal()

    PHASES = [PHASE_PROBE, PHASE_INGEST, PHASE_ALIGN, PHASE_DIFF]

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.p = palette
        self._phase_rows: dict[str, tuple[QLabel, QLabel, QLabel]] = {}
        self._current_phase = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        centre = QWidget()
        centre.setMaximumWidth(760)
        lay = QVBoxLayout(centre)
        lay.setContentsMargins(40, 60, 40, 40)
        lay.setSpacing(18)

        head = QHBoxLayout()
        head.setSpacing(12)
        self.spinner = Spinner(palette, 22)
        head.addWidget(self.spinner)
        self.title = label("Comparing…", "H2")
        head.addWidget(self.title)
        head.addStretch(1)
        lay.addLayout(head)

        self.subtitle = label("", "Muted", wrap=True)
        lay.addWidget(self.subtitle)

        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        lay.addWidget(self.bar)

        stats = QHBoxLayout()
        stats.setSpacing(10)
        self.tile_done = StatTile("Progress", "0%")
        self.tile_rate = StatTile("Throughput", "—")
        self.tile_eta = StatTile("Time left", "—")
        self.tile_elapsed = StatTile("Elapsed", "0s")
        for t in (self.tile_done, self.tile_rate, self.tile_eta,
                  self.tile_elapsed):
            stats.addWidget(t, 1)
        lay.addLayout(stats)

        phases_card = Card()
        pc = QVBoxLayout(phases_card)
        pc.setContentsMargins(20, 16, 20, 16)
        pc.setSpacing(9)
        pc.addWidget(section("Stages"))
        for phase in self.PHASES:
            row = QHBoxLayout()
            marker = QLabel("○")
            marker.setFixedWidth(18)
            marker.setStyleSheet(f"color:{palette.text_faint};")
            name = QLabel(PHASE_LABELS[phase])
            name.setStyleSheet(f"color:{palette.text_faint};")
            detail = QLabel("")
            detail.setStyleSheet(f"color:{palette.text_faint};font-size:12px;")
            detail.setAlignment(Qt.AlignmentFlag.AlignRight
                                | Qt.AlignmentFlag.AlignVCenter)
            row.addWidget(marker)
            row.addWidget(name)
            row.addStretch(1)
            row.addWidget(detail)
            pc.addLayout(row)
            self._phase_rows[phase] = (marker, name, detail)
        lay.addWidget(phases_card)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setProperty("danger", True)
        self.cancel_btn.clicked.connect(self._on_cancel)
        buttons.addWidget(self.cancel_btn)
        lay.addLayout(buttons)
        lay.addStretch(1)

        wrapper = QHBoxLayout()
        wrapper.addStretch(1)
        wrapper.addWidget(centre)
        wrapper.addStretch(1)
        root.addLayout(wrapper)

        self._elapsed = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def apply_palette(self, palette: Palette):
        self.p = palette
        # Stage rows carry inline colours rather than QSS, so repaint whichever
        # stage is active and grey out the rest.
        for phase, (marker, name, detail) in self._phase_rows.items():
            active = phase == self._current_phase
            marker.setStyleSheet(
                f"color:{palette.accent if active else palette.text_faint};")
            name.setStyleSheet(
                f"color:{palette.text if active else palette.text_faint};"
                + (" font-weight:600;" if active else ""))
            detail.setStyleSheet(f"color:{palette.text_faint};font-size:12px;")

    # -- lifecycle ---------------------------------------------------------
    def start(self, name_a: str, name_b: str):
        self.title.setText("Comparing…")
        self.subtitle.setText(f"{name_a}   ↔   {name_b}")
        self.bar.setValue(0)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")
        self.spinner.start()
        self._elapsed = 0
        self._current_phase = ""
        for phase in self.PHASES:
            marker, name, detail = self._phase_rows[phase]
            marker.setText("○")
            marker.setStyleSheet(f"color:{self.p.text_faint};")
            name.setStyleSheet(f"color:{self.p.text_faint};")
            detail.setText("")
        self.tile_done.set_value("0%")
        self.tile_rate.set_value("—")
        self.tile_eta.set_value("—")
        self.tile_elapsed.set_value("0s")
        self._timer.start(1000)

    def stop(self):
        self.spinner.stop()
        self._timer.stop()

    def _on_cancel(self):
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setText("Cancelling…")
        self.cancel_requested.emit()

    def _tick(self):
        self._elapsed += 1
        mins, secs = divmod(self._elapsed, 60)
        self.tile_elapsed.set_value(
            f"{mins}m {secs:02d}s" if mins else f"{secs}s")

    # -- updates -----------------------------------------------------------
    def update_progress(self, progress: Progress):
        phase = progress.phase
        if phase != self._current_phase:
            self._complete_previous(phase)
            self._current_phase = phase
            if phase in self._phase_rows:
                marker, name, _detail = self._phase_rows[phase]
                marker.setText("◐")
                marker.setStyleSheet(f"color:{self.p.accent};")
                name.setStyleSheet(f"color:{self.p.text}; font-weight:600;")

        if phase in self._phase_rows:
            _m, _n, detail = self._phase_rows[phase]
            detail.setText(progress.message)

        # Weight the bar by real cost: ingest dominates, align is a blip.
        weights = {PHASE_PROBE: (0.0, 0.02), PHASE_INGEST: (0.02, 0.62),
                   PHASE_ALIGN: (0.64, 0.04), PHASE_DIFF: (0.68, 0.32)}
        base, span = weights.get(phase, (0.0, 1.0))
        self.bar.setValue(int((base + span * progress.fraction) * 1000))
        self.tile_done.set_value(f"{(base + span * progress.fraction) * 100:.0f}%")

        if progress.rate > 0:
            self.tile_rate.set_value(f"{progress.rate:,.0f}/s")
        if progress.eta_s is not None and progress.eta_s > 0:
            eta = int(progress.eta_s)
            mins, secs = divmod(eta, 60)
            self.tile_eta.set_value(
                f"{mins}m {secs:02d}s" if mins else f"{secs}s")
        elif progress.phase == PHASE_DONE:
            self.tile_eta.set_value("—")

    def _complete_previous(self, new_phase: str):
        try:
            new_pos = self.PHASES.index(new_phase)
        except ValueError:
            new_pos = len(self.PHASES)
        for i, phase in enumerate(self.PHASES):
            if i < new_pos:
                marker, name, _d = self._phase_rows[phase]
                marker.setText("●")
                marker.setStyleSheet(f"color:{self.p.ok};")
                name.setStyleSheet(f"color:{self.p.text_dim};")
