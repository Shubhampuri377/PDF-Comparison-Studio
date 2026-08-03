"""Comparison orchestrator.

Phases:

    probe   -> read metadata, decide on a session directory
    ingest  -> parse every page of both documents in parallel, write to SQLite
    align   -> match pages between the documents
    diff    -> compare each aligned pair in parallel

Only ``ingest`` touches the PDFs.  That split is what makes editing an ignore
region cheap: changing compare settings re-runs ``align``/``diff`` against
already-parsed pages instead of re-reading 20,000 pages of PDF.

The engine is UI-agnostic.  It reports through a callback and stops when its
cancel event is set, so it works the same from a Qt thread, a CLI or a test.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool

from .align import align_documents, alignment_stats
from .diffcore import diff_chunk
from .extract import file_fingerprint, ingest_chunk, probe_document
from .models import (
    CompareSettings, DocumentInfo, KIND_MATCHED, Progress, RuntimeSettings,
    SIDE_A, SIDE_B, Summary,
)
from .store import ResultStore

PHASE_PROBE = "probe"
PHASE_INGEST = "ingest"
PHASE_ALIGN = "align"
PHASE_DIFF = "diff"
PHASE_DONE = "done"
PHASE_CANCELLED = "cancelled"

# Spawning a worker pool on Windows costs roughly 0.7 s before the first task
# runs, so tiny jobs finish faster in-process.  The break-even differs sharply
# by phase: parsing a page costs ~14 ms, diffing an already-parsed pair ~3 ms,
# so diff stays inline far longer than ingest does.  Both were measured rather
# than guessed -- an earlier single threshold of 250 pages made a 100-page
# comparison slower, not faster.
_INLINE_INGEST_PAGES = 64
# Distinct recoverable-warning messages kept before further ones are dropped.
_MAX_DISTINCT_WARNINGS = 40
_INLINE_DIFF_PAIRS = 400

PHASE_LABELS = {
    PHASE_PROBE: "Reading documents",
    PHASE_INGEST: "Extracting pages",
    PHASE_ALIGN: "Aligning pages",
    PHASE_DIFF: "Comparing pages",
    PHASE_DONE: "Complete",
    PHASE_CANCELLED: "Cancelled",
}


class CancelledError(Exception):
    pass


def session_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") \
        or tempfile.gettempdir()
    path = os.path.join(base, "PDFComparisonStudio", "sessions")
    os.makedirs(path, exist_ok=True)
    return path


def session_id(path_a: str, path_b: str, fp_a: str, fp_b: str) -> str:
    h = hashlib.blake2b(digest_size=10)
    for part in (os.path.basename(path_a), os.path.basename(path_b), fp_a, fp_b):
        h.update(part.encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def purge_old_sessions(keep_days: float = 7.0, keep_max: int = 12):
    """Housekeeping so the cache does not grow without bound."""
    root = session_root()
    try:
        entries = [(os.path.join(root, n), os.path.getmtime(os.path.join(root, n)))
                   for n in os.listdir(root)
                   if os.path.isdir(os.path.join(root, n))]
    except OSError:
        return
    entries.sort(key=lambda t: t[1], reverse=True)
    cutoff = time.time() - keep_days * 86400
    for i, (path, mtime) in enumerate(entries):
        if i >= keep_max or mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)


class _RateTracker:
    """Exponentially smoothed throughput, for an ETA that does not jitter."""

    def __init__(self, alpha: float = 0.25):
        self.alpha = alpha
        self.rate = 0.0
        self._last_t = None
        self._last_n = 0

    def update(self, done: int) -> float:
        now = time.monotonic()
        if self._last_t is None:
            self._last_t, self._last_n = now, done
            return 0.0
        dt = now - self._last_t
        if dt < 0.20:
            return self.rate
        inst = (done - self._last_n) / dt
        self.rate = inst if self.rate == 0.0 else \
            self.alpha * inst + (1 - self.alpha) * self.rate
        self._last_t, self._last_n = now, done
        return self.rate


class ComparisonEngine:
    """Drives one comparison run against one session directory."""

    def __init__(self, path_a: str, path_b: str,
                 settings: CompareSettings | None = None,
                 runtime: RuntimeSettings | None = None,
                 password_a: str = "", password_b: str = "",
                 progress_cb=None):
        self.path_a = path_a
        self.path_b = path_b
        self.settings = settings or CompareSettings()
        self.runtime = runtime or RuntimeSettings()
        self.password_a = password_a
        self.password_b = password_b
        self.progress_cb = progress_cb

        self.cancel_event = threading.Event()
        self.info_a: DocumentInfo | None = None
        self.info_b: DocumentInfo | None = None
        self.session_dir: str = ""
        self.db_path: str = ""
        self.store: ResultStore | None = None
        self.summary: Summary | None = None
        self.align_stats: dict = {}
        self.warnings: list[str] = []
        self._warning_keys: set = set()
        self._warning_counts: dict = {}
        self._started = 0.0
        self._pool: ProcessPoolExecutor | None = None

    # -- plumbing -----------------------------------------------------------
    def cancel(self):
        self.cancel_event.set()

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise CancelledError()

    def _emit(self, phase: str, done: int, total: int, message: str = "",
              rate: float = 0.0):
        if not self.progress_cb:
            return
        eta = None
        if rate > 0 and total > done:
            eta = (total - done) / rate
        self.progress_cb(Progress(phase=phase, done=done, total=total,
                                  message=message, rate=rate, eta_s=eta))

    # -- phases -------------------------------------------------------------
    def probe(self):
        self._emit(PHASE_PROBE, 0, 2, "Reading document A")
        a = probe_document(self.path_a, self.password_a)
        self._check_cancel()
        self._emit(PHASE_PROBE, 1, 2, "Reading document B")
        b = probe_document(self.path_b, self.password_b)

        for name, d in (("A", a), ("B", b)):
            if d.get("error"):
                raise RuntimeError(f"Document {name}: {d['error']}")
            if d.get("page_count", 0) <= 0:
                raise RuntimeError(f"Document {name} has no pages")

        self.info_a = DocumentInfo.from_dict(a)
        self.info_b = DocumentInfo.from_dict(b)

        sid = session_id(self.path_a, self.path_b,
                         self.info_a.fingerprint, self.info_b.fingerprint)
        self.session_dir = os.path.join(session_root(), sid)
        os.makedirs(self.session_dir, exist_ok=True)
        self.db_path = os.path.join(self.session_dir, "compare.db")
        self._emit(PHASE_PROBE, 2, 2, "Ready")

    def _open_store(self) -> ResultStore:
        if self.store is None:
            self.store = ResultStore(self.db_path)
        return self.store

    def _ingest_is_current(self, store: ResultStore) -> bool:
        """Can we skip parsing entirely and reuse the cached page data?"""
        if not store.schema_ok():
            return False
        if store.get_meta("fp_a") != self.info_a.fingerprint:
            return False
        if store.get_meta("fp_b") != self.info_b.fingerprint:
            return False
        # Image and formatting extraction are opt-in; a cache built without
        # them cannot answer a run that wants them.
        if store.get_meta("ingest_images") != str(int(self.settings.compare_images)):
            return False
        if store.get_meta("ingest_format") != str(int(self.settings.compare_formatting)):
            return False
        return (store.ingested_pages(SIDE_A) == self.info_a.page_count
                and store.ingested_pages(SIDE_B) == self.info_b.page_count)

    def ingest(self, force: bool = False):
        store = self._open_store()
        total = self.info_a.page_count + self.info_b.page_count

        if not force and self._ingest_is_current(store):
            self._emit(PHASE_INGEST, total, total, "Reusing cached page data")
            return

        store.init_schema_version()
        store.set_meta_many({
            "fp_a": self.info_a.fingerprint,
            "fp_b": self.info_b.fingerprint,
            "path_a": self.path_a,
            "path_b": self.path_b,
            "ingest_images": str(int(self.settings.compare_images)),
            "ingest_format": str(int(self.settings.compare_formatting)),
        })

        chunk = max(8, self.runtime.chunk_pages)
        tasks = []
        for side, info, password in ((SIDE_A, self.info_a, self.password_a),
                                     (SIDE_B, self.info_b, self.password_b)):
            for start in range(0, info.page_count, chunk):
                tasks.append({
                    "path": info.path, "side": side,
                    "start": start, "end": min(start + chunk, info.page_count),
                    "password": password,
                    "compare_images": self.settings.compare_images,
                    "image_fuzzy": self.settings.image_fuzzy,
                    "compare_formatting": self.settings.compare_formatting,
                })

        def absorb(result):
            if result["rows"]:
                store.write_pages(result["rows"])
            side_name = "A" if result["side"] == SIDE_A else "B"
            for pno, msg in result["errors"]:
                # A damaged file repeats the same complaint on every page.
                # Recording 20,000 copies of it helps nobody, so each distinct
                # message is kept once with the first page that produced it.
                key = (side_name, msg)
                if key in self._warning_keys:
                    self._warning_counts[key] = self._warning_counts.get(key, 1) + 1
                    continue
                if len(self._warning_keys) >= _MAX_DISTINCT_WARNINGS:
                    continue
                self._warning_keys.add(key)
                self._warning_counts[key] = 1
                self.warnings.append(f"Doc {side_name} page {pno + 1}: {msg}")

        self._run_tasks(ingest_chunk, tasks, PHASE_INGEST, total,
                        units=lambda t: t["end"] - t["start"],
                        on_result=absorb, inline_limit=_INLINE_INGEST_PAGES)
        self._emit(PHASE_INGEST, total, total, f"{total:,} pages extracted")

    def align(self):
        store = self._open_store()
        self._emit(PHASE_ALIGN, 0, 3, "Reading fingerprints")
        exact_a, sims_a, counts_a = store.fingerprints(SIDE_A)
        exact_b, sims_b, counts_b = store.fingerprints(SIDE_B)
        self._check_cancel()

        self._emit(PHASE_ALIGN, 1, 3, "Matching pages")
        pairs = align_documents(
            exact_a, exact_b, sims_a, sims_b, counts_a, counts_b,
            min_similarity=self.settings.align_min_similarity,
            enabled=self.settings.align_pages)
        self._check_cancel()

        self.align_stats = alignment_stats(pairs)
        self._emit(PHASE_ALIGN, 2, 3, "Writing plan")
        store.write_pairs([(i, a, b, k) for i, (a, b, k) in enumerate(pairs)])
        store.set_meta_many({
            "align_stats": repr(self.align_stats),
            "align_enabled": str(int(self.settings.align_pages)),
            "align_thresh": f"{self.settings.align_min_similarity:.3f}",
        })
        self._emit(PHASE_ALIGN, 3, 3,
                   f"{self.align_stats['matched']:,} matched, "
                   f"{self.align_stats['added']:,} added, "
                   f"{self.align_stats['removed']:,} removed")

    def diff(self, only_pending: bool = False):
        store = self._open_store()
        if not only_pending:
            store.reset_pair_results()
        indices = store.pending_pair_indices()
        total = len(indices)
        if total == 0:
            self._emit(PHASE_DIFF, 0, 0, "Nothing to compare")
            return

        settings_dict = self.settings.to_dict()
        chunk = max(16, self.runtime.chunk_pages)
        plan = store.pair_plan(indices)

        tasks = [{"db": self.db_path, "settings": settings_dict,
                  "plan": plan[i:i + chunk]}
                 for i in range(0, len(plan), chunk)]

        def absorb(result):
            if result["rows"]:
                store.write_pair_results(result["rows"])

        self._run_tasks(diff_chunk, tasks, PHASE_DIFF, total,
                        units=lambda t: len(t["plan"]),
                        on_result=absorb, inline_limit=_INLINE_DIFF_PAIRS)
        store.set_meta("settings_fp", self.settings.fingerprint())
        self._emit(PHASE_DIFF, total, total, f"{total:,} pages compared")

    # -- work dispatch ------------------------------------------------------
    def _run_tasks(self, fn, tasks: list, phase: str, total: int,
                   units, on_result, inline_limit: int):
        """Execute chunk tasks, in-process or across the pool.

        Runs inline only when there is no pool yet *and* the job is small
        enough that starting one would cost more than it saves.  Once a pool
        exists its startup is already paid for, so later phases always use it.
        """
        done = 0
        tracker = _RateTracker()
        inline = self._pool is None and (len(tasks) <= 1 or total <= inline_limit)

        if inline:
            self._emit(phase, 0, total, "Working")
            for task in tasks:
                self._check_cancel()
                on_result(fn(task))
                done += units(task)
                self._emit(phase, done, total, f"{done:,} / {total:,} pages",
                           tracker.update(done))
            return

        self._emit(phase, 0, total, "Starting workers")
        pool = self._ensure_pool()
        futures = {pool.submit(fn, t): t for t in tasks}
        try:
            for fut in as_completed(futures):
                if self.cancel_event.is_set():
                    for f in futures:
                        f.cancel()
                    raise CancelledError()
                on_result(fut.result())
                done += units(futures[fut])
                self._emit(phase, done, total, f"{done:,} / {total:,} pages",
                           tracker.update(done))
        except BrokenProcessPool as exc:
            raise self._explain_pool_failure(exc) from exc
        finally:
            futures.clear()

    # -- pool ---------------------------------------------------------------
    def _ensure_pool(self) -> ProcessPoolExecutor:
        if self._pool is None:
            self._pool = ProcessPoolExecutor(
                max_workers=self.runtime.resolved_workers())
        return self._pool

    @staticmethod
    def _explain_pool_failure(exc: BaseException) -> RuntimeError:
        """Turn ``BrokenProcessPool`` into something actionable.

        On Windows every worker re-imports the ``__main__`` module. If the
        calling script has no ``if __name__ == "__main__":`` guard the workers
        re-execute it and die, and the only symptom is a bare
        ``BrokenProcessPool`` traceback that says nothing about the cause.
        """
        main = sys.modules.get("__main__", None)
        main_file = getattr(main, "__file__", None)
        hint = ""
        if main_file in (None, "<stdin>", "<string>"):
            hint = ("\n\nThe entry point has no importable __main__ module "
                    "(running from stdin or -c). On Windows, multiprocessing "
                    "workers re-import __main__, so the engine must be driven "
                    "from a real script file guarded with:\n\n"
                    '    if __name__ == "__main__":\n        main()')
        return RuntimeError(
            f"Worker processes failed to start or died during the run "
            f"({type(exc).__name__}).{hint}")

    def shutdown_pool(self):
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    # -- top level ----------------------------------------------------------
    def run(self, force_ingest: bool = False) -> Summary:
        self._started = time.monotonic()
        try:
            self.probe()
            self._check_cancel()
            self.ingest(force=force_ingest)
            self._check_cancel()
            self.align()
            self._check_cancel()
            self.diff()
        except CancelledError:
            self._emit(PHASE_CANCELLED, 0, 0, "Cancelled")
            raise
        finally:
            self.shutdown_pool()

        store = self._open_store()
        self.summary = store.summary()
        self.summary.duration_s = time.monotonic() - self._started
        store.set_meta_many({
            "duration_s": f"{self.summary.duration_s:.3f}",
            "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self._emit(PHASE_DONE, 1, 1, "Complete")
        return self.summary

    def rediff(self, settings: CompareSettings) -> Summary:
        """Re-apply new compare settings without re-parsing the PDFs.

        This is what makes the ignore-area editor usable: draw a box, and only
        the diff phase runs again.
        """
        self._started = time.monotonic()
        self.settings = settings
        try:
            # Ignore regions and normalisation never change page identity, but
            # the alignment toggle and threshold do.
            store = self._open_store()
            if store.get_meta("align_enabled") != str(int(settings.align_pages)) or \
               store.get_meta("align_thresh") != f"{settings.align_min_similarity:.3f}":
                self.align()
                store.set_meta_many({
                    "align_enabled": str(int(settings.align_pages)),
                    "align_thresh": f"{settings.align_min_similarity:.3f}",
                })
            self._check_cancel()
            self.diff()
        except CancelledError:
            self._emit(PHASE_CANCELLED, 0, 0, "Cancelled")
            raise
        finally:
            self.shutdown_pool()

        store = self._open_store()
        self.summary = store.summary()
        self.summary.duration_s = time.monotonic() - self._started
        self._emit(PHASE_DONE, 1, 1, "Complete")
        return self.summary

    def warning_summary(self) -> list[str]:
        """Warnings with an occurrence count, most frequent first."""
        out = []
        for (side, msg), n in sorted(self._warning_counts.items(),
                                     key=lambda kv: -kv[1]):
            suffix = f"  (x{n:,} pages)" if n > 1 else ""
            out.append(f"Doc {side}: {msg}{suffix}")
        return out

    def close(self):
        self.shutdown_pool()
        if self.store is not None:
            self.store.close()
