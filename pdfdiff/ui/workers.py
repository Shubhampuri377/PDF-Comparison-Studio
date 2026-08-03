"""Background threads.

Every long operation runs off the GUI thread and reports back through Qt
signals.  The old build mutated Tk widgets straight from a worker thread,
which is exactly the kind of thing that produces irreproducible freezes; Qt's
queued connections make the correct pattern the easy one.
"""
from __future__ import annotations

import os
import traceback
from collections import OrderedDict

from PySide6.QtCore import (QMutex, QMutexLocker, QObject, QThread,
                            QWaitCondition, Qt, Signal)
from PySide6.QtGui import QImage

from ..engine import CancelledError, ComparisonEngine
from ..models import CompareSettings, DocumentInfo, Progress, RuntimeSettings
from ..render import PageRenderer, RenderedPage


# --------------------------------------------------------------------------
def to_qimage(rendered: RenderedPage) -> QImage:
    """Wrap raw RGB888 samples in a QImage.

    ``.copy()`` is essential: the QImage would otherwise alias the bytes owned
    by the PyMuPDF pixmap, which are freed as soon as it goes out of scope.
    """
    img = QImage(rendered.samples, rendered.width, rendered.height,
                 rendered.stride, QImage.Format.Format_RGB888)
    return img.copy()


class ImageCache:
    """Byte-bounded LRU of rendered pages, holding QImage rather than QPixmap.

    QPixmap may only be created and touched on the GUI thread -- Qt is explicit
    about this, and violating it produces an access violation at an
    unpredictable moment rather than a clean error. Rendering happens on a
    worker thread, so everything here stays QImage; the conversion to QPixmap
    happens in the GUI-thread slot that receives it.
    """

    def __init__(self, max_bytes: int = 384 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._items: OrderedDict[tuple, QImage] = OrderedDict()
        self._bytes = 0
        self._mutex = QMutex()

    @staticmethod
    def _cost(img: QImage) -> int:
        try:
            return max(1, int(img.sizeInBytes()))
        except Exception:
            return max(1, img.width() * img.height() * 4)

    def get(self, key) -> QImage | None:
        with QMutexLocker(self._mutex):
            img = self._items.get(key)
            if img is not None:
                self._items.move_to_end(key)
            return img

    def put(self, key, img: QImage):
        with QMutexLocker(self._mutex):
            if key in self._items:
                self._bytes -= self._cost(self._items[key])
            self._items[key] = img
            self._items.move_to_end(key)
            self._bytes += self._cost(img)
            while self._bytes > self.max_bytes and len(self._items) > 1:
                _k, old = self._items.popitem(last=False)
                self._bytes -= self._cost(old)

    def clear(self):
        with QMutexLocker(self._mutex):
            self._items.clear()
            self._bytes = 0

    @property
    def usage_mb(self) -> float:
        return self._bytes / (1024 * 1024)


# --------------------------------------------------------------------------
class EngineWorker(QThread):
    """Runs a full comparison, or a re-diff, without blocking the UI."""

    progressed = Signal(object)      # Progress
    finished_ok = Signal(object)     # Summary
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, path_a: str, path_b: str, settings: CompareSettings,
                 runtime: RuntimeSettings, password_a: str = "",
                 password_b: str = "", engine: ComparisonEngine | None = None,
                 mode: str = "run", parent=None):
        super().__init__(parent)
        self.mode = mode
        self.settings = settings
        if engine is not None:
            self.engine = engine
            engine.progress_cb = self._on_progress
        else:
            self.engine = ComparisonEngine(
                path_a, path_b, settings=settings, runtime=runtime,
                password_a=password_a, password_b=password_b,
                progress_cb=self._on_progress)

    def _on_progress(self, progress: Progress):
        self.progressed.emit(progress)

    def cancel(self):
        self.engine.cancel()

    def run(self):
        try:
            if self.mode == "rediff":
                summary = self.engine.rediff(self.settings)
            else:
                summary = self.engine.run()
            self.finished_ok.emit(summary)
        except CancelledError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}\n\n"
                             f"{traceback.format_exc(limit=6)}")


# --------------------------------------------------------------------------
class RenderRequest:
    __slots__ = ("token", "row", "a_pno", "b_pno", "scale", "kind")

    def __init__(self, token: int, row: int, a_pno, b_pno, scale: float,
                 kind: str = "page"):
        self.token = token
        self.row = row
        self.a_pno = a_pno
        self.b_pno = b_pno
        self.scale = scale
        self.kind = kind


class RenderService(QThread):
    """Serialised page rasteriser with newest-request-wins scheduling.

    Holding only the most recent page request means a user dragging through
    the page list never queues up 400 renders they will not look at.
    Thumbnails queue separately and are drained oldest-first.
    """

    # QImage, not QPixmap: these cross a thread boundary.
    page_ready = Signal(int, int, object, object)   # token, row, QImage|None x2
    thumb_ready = Signal(int, int, object)          # token, row, QImage|None

    def __init__(self, path_a: str, path_b: str, password_a: str = "",
                 password_b: str = "", cache_mb: int = 384, parent=None):
        super().__init__(parent)
        self._renderer = PageRenderer(path_a, path_b, password_a, password_b)
        self.cache = ImageCache(cache_mb * 1024 * 1024)
        self._mutex = QMutex()
        self._wait = QWaitCondition()
        self._pending: RenderRequest | None = None
        self._thumbs: list[RenderRequest] = []
        self._running = True

    # -- public ------------------------------------------------------------
    def request_page(self, token: int, row: int, a_pno, b_pno, scale: float):
        with QMutexLocker(self._mutex):
            self._pending = RenderRequest(token, row, a_pno, b_pno, scale)
            self._wait.wakeAll()

    def request_thumb(self, token: int, row: int, a_pno, b_pno, px: int):
        with QMutexLocker(self._mutex):
            # Cap the backlog; the list view re-requests whatever is still
            # visible after a fast scroll settles.
            if len(self._thumbs) > 96:
                self._thumbs = self._thumbs[-48:]
            self._thumbs.append(
                RenderRequest(token, row, a_pno, b_pno, float(px), "thumb"))
            self._wait.wakeAll()

    def drop_thumbs(self):
        with QMutexLocker(self._mutex):
            self._thumbs.clear()

    def stop(self):
        """Shut down, then release the PDF handles.

        The documents must not be closed until the render thread has actually
        finished: freeing MuPDF structures while that thread is still inside
        them is the other reliable way to produce an access violation.
        """
        with QMutexLocker(self._mutex):
            self._running = False
            self._wait.wakeAll()
        if not self.wait(10000):
            # Refuse to free memory the thread may still be reading.
            self.terminate()
            self.wait(2000)
            return
        self.cache.clear()
        self._renderer.close()

    # -- thread body -------------------------------------------------------
    def run(self):
        while True:
            with QMutexLocker(self._mutex):
                while self._running and self._pending is None and not self._thumbs:
                    self._wait.wait(self._mutex)
                if not self._running:
                    return
                if self._pending is not None:
                    request = self._pending
                    self._pending = None
                else:
                    request = self._thumbs.pop(0)

            try:
                if request.kind == "thumb":
                    self._do_thumb(request)
                else:
                    self._do_page(request)
            except Exception:
                # A single unrenderable page must not take the service down.
                if request.kind == "thumb":
                    self.thumb_ready.emit(request.token, request.row, None)
                else:
                    self.page_ready.emit(request.token, request.row, None, None)

    def _render_side(self, side: int, pno, scale: float) -> QImage | None:
        if pno is None:
            return None
        # Bucket the scale so small zoom nudges reuse an existing bitmap.
        bucket = round(scale, 2)
        key = (side, pno, bucket)
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        rendered = self._renderer.render(side, pno, bucket)
        if rendered is None:
            return None
        img = to_qimage(rendered)
        self.cache.put(key, img)
        return img

    def _do_page(self, request: RenderRequest):
        left = self._render_side(0, request.a_pno, request.scale)
        right = self._render_side(1, request.b_pno, request.scale)
        self.page_ready.emit(request.token, request.row, left, right)

    def _do_thumb(self, request: RenderRequest):
        px = int(request.scale)
        side = 0 if request.a_pno is not None else 1
        pno = request.a_pno if request.a_pno is not None else request.b_pno
        key = ("thumb", side, pno, px)
        cached = self.cache.get(key)
        if cached is None:
            rendered = self._renderer.render_thumbnail(side, pno, px)
            if rendered is None:
                self.thumb_ready.emit(request.token, request.row, None)
                return
            cached = to_qimage(rendered)
            self.cache.put(key, cached)
        self.thumb_ready.emit(request.token, request.row, cached)


# --------------------------------------------------------------------------
class ReportWorker(QThread):
    """Writes report files off the GUI thread."""

    progressed = Signal(int, int)
    finished_ok = Signal(list)
    failed = Signal(str)

    def __init__(self, output_dir: str, basename: str, db_path: str,
                 info_a: DocumentInfo, info_b: DocumentInfo, summary,
                 options, path_a: str, path_b: str,
                 settings_dict: dict, parent=None):
        super().__init__(parent)
        self.output_dir = output_dir
        self.basename = basename
        self.db_path = db_path
        self.info_a = info_a
        self.info_b = info_b
        self.summary = summary
        self.options = options
        self.path_a = path_a
        self.path_b = path_b
        self.settings_dict = settings_dict

    def run(self):
        from ..reporting import generate
        from ..store import ResultStore

        store = None
        renderer = None
        try:
            store = ResultStore(self.db_path, readonly=True)
            renderer = PageRenderer(self.path_a, self.path_b)
            paths = generate(self.output_dir, self.basename, store,
                             self.info_a, self.info_b, self.summary,
                             self.options, renderer, self.settings_dict,
                             progress_cb=lambda d, t: self.progressed.emit(d, t))
            self.finished_ok.emit(paths)
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__}: {exc}\n\n"
                             f"{traceback.format_exc(limit=6)}")
        finally:
            if renderer is not None:
                renderer.close()
            if store is not None:
                store.close()


# --------------------------------------------------------------------------
class ProbeWorker(QThread):
    """Reads PDF metadata for the home screen without stalling the UI."""

    done = Signal(int, dict)     # slot (0/1), probe dict

    def __init__(self, slot: int, path: str, password: str = "", parent=None):
        super().__init__(parent)
        self.slot = slot
        self.path = path
        self.password = password

    def run(self):
        from ..extract import probe_document

        try:
            self.done.emit(self.slot, probe_document(self.path, self.password))
        except Exception as exc:
            self.done.emit(self.slot, {"path": self.path, "error": str(exc)})


class LicenceWatchdog(QThread):
    """Re-validates the licence on an interval.

    The previous build defined a watchdog and never started it, so a licence
    that expired mid-session stayed usable until restart.
    """

    invalid = Signal(str)
    tick = Signal(object)        # LicenceStatus

    def __init__(self, interval_s: int = 60, parent=None):
        super().__init__(parent)
        self.interval_s = interval_s
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        from ..licensing import current_status

        while self._running:
            for _ in range(self.interval_s * 4):
                if not self._running:
                    return
                self.msleep(250)
            status = current_status()
            self.tick.emit(status)
            if not status.valid:
                self.invalid.emit(status.reason)
                return
