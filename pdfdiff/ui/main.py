"""Application shell: window chrome, screen switching, worker lifecycle."""
from __future__ import annotations

import json
import os
import sys

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (QAction, QColor, QIcon, QKeySequence,
                           QPainter, QPixmap, QShortcut)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel,
                               QMainWindow, QMessageBox, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..engine import ComparisonEngine, purge_old_sessions
from ..models import CompareSettings, RuntimeSettings
from ..store import FILTER_ALL, ResultStore
from .dialogs import LicenceDialog, ReportDialog, SettingsDialog
from .home import HomePage, ProgressPage
from .results import ResultsPage
from .theme import (DARK, LIGHT, Palette, moon_icon, qpalette,
                    stylesheet, sun_icon, ui_font)
from .widgets import Pill, Toast, label, propagate_palette
from .workers import (EngineWorker, LicenceWatchdog, ProbeWorker, RenderService,
                      ReportWorker)

ORG = "Belwo"
APP_KEY = "PDFComparisonStudio"

# Theme used on first run, before the user has expressed a preference.
DEFAULT_THEME = "light"


_ICON_CACHE: QIcon | None = None


def _icon_path() -> str | None:
    """Locate assets/icon.ico in both a source tree and a frozen bundle."""
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(getattr(sys, "_MEIPASS", ""))
    roots.append(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    for root in roots:
        candidate = os.path.join(root, "assets", "icon.ico")
        if root and os.path.exists(candidate):
            return candidate
    return None


def app_icon() -> QIcon:
    """The application mark, from assets/icon.ico when it is available.

    Falls back to drawing the same shape so a source checkout without the
    asset still shows something sensible rather than a blank window icon.
    """
    global _ICON_CACHE
    if _ICON_CACHE is not None:
        return _ICON_CACHE

    path = _icon_path()
    if path:
        icon = QIcon(path)
        if not icon.isNull():
            _ICON_CACHE = icon
            return icon

    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#4f8cff"))
    p.drawRoundedRect(4, 4, 34, 46, 6, 6)
    p.setBrush(QColor("#22b26e"))
    p.drawRoundedRect(26, 14, 34, 46, 6, 6)
    p.end()
    _ICON_CACHE = QIcon(pm)
    return _ICON_CACHE


def apply_native_icon(widget) -> bool:
    """Attach the .ico to the native window handle via WM_SETICON.

    Qt's ``setWindowIcon`` is enough for the title bar, but the Windows
    taskbar reads the icon from the window handle, and for a process whose
    executable is python.exe it frequently falls back to a generic icon
    instead. Loading the .ico as real HICONs and sending them to the HWND is
    what actually puts the mark on the taskbar button.

    Both sizes are set: ICON_SMALL drives the title bar and the taskbar's
    small overlay, ICON_BIG the Alt-Tab switcher and large taskbar icons.
    """
    if sys.platform != "win32":
        return False
    path = _icon_path()
    if not path:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                      wintypes.UINT, ctypes.c_int,
                                      ctypes.c_int, wintypes.UINT]
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        SM_CXSMICON, SM_CXICON = 49, 11

        hwnd = int(widget.winId())
        sent = False
        for which, metric in ((ICON_SMALL, SM_CXSMICON), (ICON_BIG, SM_CXICON)):
            size = user32.GetSystemMetrics(metric) or 0
            flags = LR_LOADFROMFILE | (LR_DEFAULTSIZE if not size else 0)
            hicon = user32.LoadImageW(None, path, IMAGE_ICON, size, size, flags)
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, which, hicon)
                sent = True
        return sent
    except Exception:
        return False


class MainWindow(QMainWindow):
    PAGE_HOME = 0
    PAGE_PROGRESS = 1
    PAGE_RESULTS = 2

    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1560, 950)
        self.setMinimumSize(1080, 680)

        self.qsettings = QSettings(ORG, APP_KEY)
        # Light is the default; a stored preference from a previous session
        # still wins, so toggling is remembered.
        self.palette_ = DARK if self.qsettings.value(
            "theme", DEFAULT_THEME) == "dark" else LIGHT

        self.settings = self._load_settings()
        self.runtime = self._load_runtime()

        self.engine: ComparisonEngine | None = None
        self.worker: EngineWorker | None = None
        self.render_service: RenderService | None = None
        self.report_worker: ReportWorker | None = None
        self._probe_workers: dict[int, ProbeWorker] = {}
        self.summary = None
        self.licence_status = None
        self.watchdog: LicenceWatchdog | None = None

        self._build_ui()
        self._apply_theme()

        self.toast = Toast(self, self.palette_)
        QTimer.singleShot(0, self._restore_geometry)
        QTimer.singleShot(120, self._check_licence)
        QTimer.singleShot(600, lambda: purge_old_sessions())

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_top_bar())

        self.stack = QStackedWidget()
        self.home_page = HomePage(self.palette_, self.settings)
        self.progress_page = ProgressPage(self.palette_)
        self.results_page = ResultsPage(self.palette_)
        self.stack.addWidget(self.home_page)
        self.stack.addWidget(self.progress_page)
        self.stack.addWidget(self.results_page)
        root.addWidget(self.stack, 1)

        root.addWidget(self._build_status_bar())
        self.setCentralWidget(central)

        self.home_page.compare_requested.connect(self.start_comparison)
        self.home_page.settings_requested.connect(self.open_settings)
        self.home_page.files_changed.connect(self._probe_files)
        self.progress_page.cancel_requested.connect(self.cancel_comparison)
        self.results_page.export_requested.connect(self.open_report_dialog)
        self.results_page.new_comparison_requested.connect(self.go_home)
        self.results_page.settings_requested.connect(self.open_settings)
        self.results_page.rediff_requested.connect(self.start_rediff)

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("TopBar")
        bar.setFixedHeight(50)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 14, 0)
        lay.setSpacing(11)

        mark = QLabel()
        mark.setPixmap(app_icon().pixmap(22, 22))
        lay.addWidget(mark)

        title = label(APP_NAME, "H3")
        lay.addWidget(title)
        version = label(f"v{__version__}", "Faint")
        lay.addWidget(version)
        lay.addStretch(1)

        self.licence_pill = Pill("Checking licence…", self.palette_.text_dim)
        self.licence_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.licence_pill)

        self.licence_btn = QPushButton("Licence")
        self.licence_btn.setProperty("flat", True)
        self.licence_btn.clicked.connect(self.open_licence_dialog)
        lay.addWidget(self.licence_btn)

        # Labelled, not a bare glyph: the previous "◐" rendered as an almost
        # invisible dot and nobody could find the theme switch.
        self.theme_btn = QPushButton()
        self.theme_btn.setProperty("flat", True)
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.setIconSize(QSize(16, 16))
        self.theme_btn.clicked.connect(self.toggle_theme)
        lay.addWidget(self.theme_btn)
        self._refresh_theme_button()

        QShortcut(QKeySequence("Ctrl+Shift+L"), self,
                  activated=self.toggle_theme)
        return bar

    def _refresh_theme_button(self):
        """Label the button with the theme it will switch *to*."""
        going_light = self.palette_.name == "dark"
        target = LIGHT if going_light else DARK
        self.theme_btn.setText("  Light" if going_light else "  Dark")
        self.theme_btn.setIcon(
            sun_icon(target.warn if going_light else self.palette_.text_dim)
            if going_light
            else moon_icon(self.palette_.text_dim))
        self.theme_btn.setToolTip(
            f"Switch to the {target.name} theme  (Ctrl+Shift+L)")

    def _build_status_bar(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("StatusBar")
        bar.setFixedHeight(28)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.setSpacing(14)

        self.status_label = label("Ready", "Faint")
        lay.addWidget(self.status_label)
        lay.addStretch(1)
        self.memory_label = label("", "Faint")
        lay.addWidget(self.memory_label)
        self.session_label = label("", "Faint")
        lay.addWidget(self.session_label)

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start(2000)
        return bar

    def _refresh_status(self):
        if self.render_service is not None:
            self.memory_label.setText(
                f"Image cache {self.render_service.cache.usage_mb:.0f} MB")

    # ------------------------------------------------------------------
    # Theme
    # ------------------------------------------------------------------
    def _apply_theme(self):
        app = QApplication.instance()
        # QPalette first: item views read text colour from it, and QSS is
        # layered on top for everything else.
        app.setPalette(qpalette(self.palette_))
        app.setStyleSheet(stylesheet(self.palette_))
        app.setFont(ui_font(10))

    def toggle_theme(self):
        self.palette_ = LIGHT if self.palette_.name == "dark" else DARK
        self.qsettings.setValue("theme", self.palette_.name)
        self._apply_theme()
        # QSS covers the chrome; self-painting widgets (page list, viewer
        # canvas, drop zones) hold a Palette and need it pushed to them.
        propagate_palette(self, self.palette_)
        self._refresh_theme_button()
        self._update_licence_pill(self.licence_status)
        self.toast.show_message(
            f"Switched to the {self.palette_.name} theme.", "ok")

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------
    def _load_runtime(self) -> RuntimeSettings:
        raw = self.qsettings.value("runtime_settings", "")
        if raw:
            try:
                return RuntimeSettings.from_dict(json.loads(raw))
            except Exception:
                pass
        return RuntimeSettings()

    def _save_settings(self):
        # Exclusion rectangles are drawn against one specific pair of
        # documents, so carrying them into the next comparison -- or into the
        # next launch -- silently suppresses differences the user never asked
        # to hide. Everything else in the settings is a genuine preference and
        # is persisted.
        persisted = self.settings.to_dict()
        persisted["ignore_rules"] = []
        self.qsettings.setValue("compare_settings", json.dumps(persisted))
        self.qsettings.setValue("runtime_settings",
                                json.dumps(self.runtime.to_dict()))

    def _load_settings(self) -> CompareSettings:
        raw = self.qsettings.value("compare_settings", "")
        if raw:
            try:
                data = json.loads(raw)
                data["ignore_rules"] = []      # never restored across launches
                return CompareSettings.from_dict(data)
            except Exception:
                pass
        return CompareSettings()

    def _restore_geometry(self):
        geo = self.qsettings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        else:
            self.showMaximized()

    # ------------------------------------------------------------------
    # Licence
    # ------------------------------------------------------------------
    def _check_licence(self):
        from ..licensing import current_status

        status = current_status()
        self.licence_status = status
        self._update_licence_pill(status)
        if not status.valid:
            dialog = LicenceDialog(self.palette_, status, self)
            dialog.exec()
            status = dialog.status() or current_status()
            self.licence_status = status
            self._update_licence_pill(status)
            if not status.valid:
                QMessageBox.critical(
                    self, "Licence required",
                    f"{APP_NAME} needs a valid licence to run.\n\n"
                    f"{status.reason}")
                QTimer.singleShot(0, self.close)
                return

        # The old build defined a watchdog and never started it, so an expiry
        # mid-session went unnoticed until the next launch.
        self.watchdog = LicenceWatchdog(60, self)
        self.watchdog.tick.connect(self._update_licence_pill)
        self.watchdog.invalid.connect(self._on_licence_invalid)
        self.watchdog.start()

    def _update_licence_pill(self, status):
        if status is None or not status.valid:
            self.licence_pill.set_state("Unlicensed", self.palette_.danger)
            return
        days = status.days_left
        if days <= 3:
            colour = self.palette_.danger
        elif days <= 14:
            colour = self.palette_.warn
        else:
            colour = self.palette_.ok
        text = (f"{status.licensee} · {status.countdown()}" if days < 1
                else f"{status.licensee} · {days}d left")
        self.licence_pill.set_state(text, colour)

    def _on_licence_invalid(self, reason: str):
        QMessageBox.critical(self, "Licence", f"Licence is no longer valid.\n\n{reason}")
        self.close()

    def open_licence_dialog(self):
        dialog = LicenceDialog(self.palette_, self.licence_status, self)
        dialog.exec()
        from ..licensing import current_status

        self.licence_status = current_status()
        self._update_licence_pill(self.licence_status)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------
    def go_home(self):
        self._teardown_run()
        self.home_page.refresh_from_settings()
        self.stack.setCurrentIndex(self.PAGE_HOME)
        self.status_label.setText("Ready")
        self.session_label.setText("")

    def open_settings(self):
        dialog = SettingsDialog(self.settings, self.runtime, self.palette_, self)
        if dialog.exec() != SettingsDialog.DialogCode.Accepted:
            return
        old = self.settings
        self.settings = dialog.result_settings()
        self.runtime = dialog.result_runtime()
        self._save_settings()
        self.home_page.settings = self.settings
        self.home_page.refresh_from_settings()

        if self.stack.currentIndex() == self.PAGE_RESULTS and self.engine:
            needs_reingest = (
                old.compare_images != self.settings.compare_images
                or old.compare_formatting != self.settings.compare_formatting)
            if needs_reingest:
                self.toast.show_message(
                    "Image or formatting extraction changed — re-running the "
                    "full comparison.", "info")
                self.start_comparison(reuse_paths=True)
            else:
                self.start_rediff(self.results_page.ignore_rules())

    # ------------------------------------------------------------------
    # Probing
    # ------------------------------------------------------------------
    def _probe_files(self):
        for slot, path in enumerate(self.home_page.paths()):
            if not path:
                continue
            worker = self._probe_workers.get(slot)
            if worker is not None and worker.isRunning():
                continue
            worker = ProbeWorker(slot, path, parent=self)
            worker.done.connect(self.home_page.set_probe)
            self._probe_workers[slot] = worker
            worker.start()

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------
    def start_comparison(self, reuse_paths: bool = False):
        path_a, path_b = self.home_page.paths()
        if not path_a or not path_b:
            return
        self._teardown_run(keep_paths=True)
        # Rectangles were drawn over the previous document pair; they do not
        # carry to a different one.
        self.settings.ignore_rules = []
        self.results_page.clear_ignore_rules()

        self.progress_page.start(os.path.basename(path_a),
                                 os.path.basename(path_b))
        self.stack.setCurrentIndex(self.PAGE_PROGRESS)
        self.status_label.setText("Comparing…")

        # parent=self keeps the QThread alive for its whole run; an unparented
        # thread object can be collected out from under a running thread.
        self.worker = EngineWorker(path_a, path_b, self.settings, self.runtime,
                                   parent=self)
        self.engine = self.worker.engine
        self.worker.progressed.connect(self.progress_page.update_progress)
        self.worker.finished_ok.connect(self._on_comparison_done)
        self.worker.failed.connect(self._on_comparison_failed)
        self.worker.cancelled.connect(self._on_comparison_cancelled)
        self.worker.start()

    def start_rediff(self, ignore_rules: list):
        if self.engine is None:
            return
        settings = CompareSettings.from_dict(self.settings.to_dict())
        settings.ignore_rules = ignore_rules
        self.settings.ignore_rules = ignore_rules
        self._save_settings()

        self.results_page.set_busy(True)
        self.status_label.setText("Re-comparing with updated rules…")

        self.worker = EngineWorker("", "", settings, self.runtime,
                                   engine=self.engine, mode="rediff",
                                   parent=self)
        self.worker.progressed.connect(self._on_rediff_progress)
        self.worker.finished_ok.connect(self._on_rediff_done)
        self.worker.failed.connect(self._on_comparison_failed)
        self.worker.cancelled.connect(
            lambda: self.results_page.set_busy(False))
        self.worker.start()

    def _on_rediff_progress(self, progress):
        if progress.total:
            self.status_label.setText(
                f"Re-comparing — {progress.message} "
                f"({progress.fraction * 100:.0f}%)")

    def _on_rediff_done(self, summary):
        self.summary = summary
        self.results_page.set_busy(False)
        self.results_page.refresh_after_rediff(summary)
        self.status_label.setText("Ready")
        self.toast.show_message(
            f"Updated — {summary.changed:,} pages differ "
            f"({summary.total_changes:,} changes) in {summary.duration_s:.1f}s.",
            "ok")

    def cancel_comparison(self):
        if self.worker is not None:
            self.worker.cancel()

    def _on_comparison_done(self, summary):
        self.summary = summary
        self.progress_page.stop()

        engine = self.engine
        self.render_service = RenderService(
            engine.path_a, engine.path_b,
            cache_mb=self.runtime.render_cache_mb, parent=self)
        self.render_service.start()

        store = ResultStore(engine.db_path, readonly=True)
        self.results_page.attach(
            store, summary, self.render_service,
            os.path.basename(engine.path_a), os.path.basename(engine.path_b),
            self.settings.ignore_rules)
        self.stack.setCurrentIndex(self.PAGE_RESULTS)

        self.status_label.setText("Ready")
        self.session_label.setText(
            f"Session {os.path.basename(engine.session_dir)}")

        if engine.warnings:
            # These are recoverable: the pages parsed, the comparison is
            # valid, the file just is not perfectly well formed.
            first = engine.warning_summary()[:1]
            detail = f" — {first[0]}" if first else ""
            self.toast.show_message(
                f"Completed. {len(engine.warnings)} recoverable issue(s) in "
                f"the source files{detail}", "warn", 7000)
            self.status_label.setText(
                f"Ready — {len(engine.warnings)} document warning(s)")
        elif summary.changed == 0:
            self.toast.show_message(
                f"No differences found across {summary.total_pairs:,} pages.",
                "ok", 5000)
        else:
            bits = [f"{summary.changed:,} pages differ"]
            if summary.added_pages:
                bits.append(f"{summary.added_pages:,} added")
            if summary.removed_pages:
                bits.append(f"{summary.removed_pages:,} removed")
            self.toast.show_message(
                " · ".join(bits) + f" · {summary.duration_s:.1f}s", "warn", 5000)

    def _on_comparison_failed(self, message: str):
        self.progress_page.stop()
        self.results_page.set_busy(False)
        self.stack.setCurrentIndex(self.PAGE_HOME)
        self.status_label.setText("Comparison failed")
        QMessageBox.critical(self, "Comparison failed", message)

    def _on_comparison_cancelled(self):
        self.progress_page.stop()
        self.stack.setCurrentIndex(self.PAGE_HOME)
        self.status_label.setText("Cancelled")
        self.toast.show_message("Comparison cancelled.", "info")

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------
    def open_report_dialog(self):
        if self.engine is None or self.summary is None:
            return
        default_dir = self.qsettings.value(
            "report_dir", os.path.join(os.path.expanduser("~"), "Documents"))
        base = (f"{os.path.splitext(os.path.basename(self.engine.path_a))[0]}"
                f"_vs_"
                f"{os.path.splitext(os.path.basename(self.engine.path_b))[0]}")

        dialog = ReportDialog(self.palette_, default_dir, base[:80],
                              self.summary.changed,
                              ignore_rules=self.results_page.ignore_rules(),
                              parent=self)
        dialog.export_btn.clicked.connect(
            lambda: self._run_report(dialog))
        dialog.exec()

    def _run_report(self, dialog: ReportDialog):
        options = dialog.options()
        if not options.formats:
            QMessageBox.information(self, "Export report",
                                    "Choose at least one output format.")
            return
        out_dir = dialog.output_dir()
        if not out_dir:
            QMessageBox.information(self, "Export report",
                                    "Choose an output folder.")
            return
        self.qsettings.setValue("report_dir", out_dir)
        dialog.set_busy(True)

        self.report_worker = ReportWorker(
            out_dir, dialog.basename(), self.engine.db_path,
            self.engine.info_a, self.engine.info_b, self.summary, options,
            self.engine.path_a, self.engine.path_b,
            self.settings.to_dict(), parent=self)
        self.report_worker.finished_ok.connect(
            lambda paths: self._on_report_done(dialog, paths))
        self.report_worker.failed.connect(
            lambda msg: self._on_report_failed(dialog, msg))
        self.report_worker.start()

    def _on_report_done(self, dialog: ReportDialog, paths: list):
        dialog.set_busy(False)
        dialog.accept()
        self.toast.show_message(
            f"Wrote {len(paths)} file(s) to {os.path.dirname(paths[0])}",
            "ok", 5000)
        if paths:
            try:
                os.startfile(os.path.dirname(paths[0]))    # noqa: S606
            except (AttributeError, OSError):
                pass

    def _on_report_failed(self, dialog: ReportDialog, message: str):
        dialog.set_busy(False)
        QMessageBox.critical(self, "Export failed", message)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def _teardown_run(self, keep_paths: bool = False):
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(4000)
            self.worker = None
        if self.render_service is not None:
            self.render_service.stop()
            self.render_service = None
        if self.engine is not None:
            self.engine.close()
            self.engine = None
        self.summary = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "toast") and self.toast.isVisible():
            self.toast._reposition()

    def closeEvent(self, event):
        self.qsettings.setValue("geometry", self.saveGeometry())
        self._save_settings()
        if self.watchdog is not None:
            self.watchdog.stop()
            self.watchdog.wait(1500)
        for worker in self._probe_workers.values():
            worker.wait(1000)
        if self.report_worker is not None:
            self.report_worker.wait(2000)
        self._teardown_run()
        super().closeEvent(event)


def run() -> int:
    # Windows taskbar grouping wants an explicit AppUserModelID.
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                f"{ORG}.{APP_KEY}.{__version__}")
        except Exception:
            pass

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG)
    app.setWindowIcon(app_icon())

    window = MainWindow()
    window.show()
    # After show(): winId() has to return a real HWND for WM_SETICON to land.
    apply_native_icon(window)
    return app.exec()
