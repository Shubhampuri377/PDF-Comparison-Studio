"""Scale benchmark: build a large PDF pair and time a full comparison.

    python -m tests.benchmark 20000

Reports wall time per phase, throughput, peak RSS and the on-disk session
size.  Peak RSS is the number that matters: it must stay flat as page count
grows, otherwise the design has an in-memory list somewhere it should not.
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pdfdiff.engine import ComparisonEngine, PHASE_LABELS       # noqa: E402
from pdfdiff.models import CompareSettings, RuntimeSettings     # noqa: E402
from tests import make_fixtures                                 # noqa: E402


def _python_tree_rss_mb() -> float:
    """Total working set of every python.exe, parent plus workers.

    The parent is the process that would leak if anything accumulated per
    page, but the honest scalability number is the whole pool, so this sums
    them.  Parsed from ``tasklist`` because it needs no third-party package.
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return 0.0
    total_kb = 0
    for row in csv.reader(io.StringIO(out)):
        if len(row) < 5:
            continue
        digits = "".join(ch for ch in row[4] if ch.isdigit())
        if digits:
            total_kb += int(digits)
    return total_kb / 1024.0


class PeakMemory(threading.Thread):
    """Sample the working set of the whole python process tree."""

    def __init__(self, interval: float = 1.0):
        super().__init__(daemon=True)
        self.interval = interval
        self.peak_mb = 0.0
        self.peak_parent_mb = 0.0
        self._stop = threading.Event()
        self._parent_probe = self._pick_parent_probe()

    @staticmethod
    def _pick_parent_probe():
        """Working set of this process.

        ``GetProcessMemoryInfo`` lives in psapi.dll but on Win10+ the real
        export is ``K32GetProcessMemoryInfo`` in kernel32; the psapi copy is a
        forwarder that fails quietly and returns zero if argtypes are wrong.
        Both are tried, and the result is validated before being trusted.
        """
        try:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            handle = kernel32.GetCurrentProcess()

            fn = None
            for lib, name in ((kernel32, "K32GetProcessMemoryInfo"),
                              (ctypes.WinDLL("psapi.dll"),
                               "GetProcessMemoryInfo")):
                candidate = getattr(lib, name, None)
                if candidate is None:
                    continue
                candidate.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC),
                                      wintypes.DWORD]
                candidate.restype = wintypes.BOOL
                fn = candidate
                break
            if fn is None:
                return None

            def probe():
                c = PMC()
                c.cb = ctypes.sizeof(PMC)
                if not fn(handle, ctypes.byref(c), ctypes.sizeof(PMC)):
                    return 0.0
                return c.WorkingSetSize / (1024 * 1024)

            return probe if probe() > 1.0 else None
        except Exception:
            return None

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                if self._parent_probe is not None:
                    self.peak_parent_mb = max(self.peak_parent_mb,
                                              self._parent_probe())
                self.peak_mb = max(self.peak_mb, _python_tree_rss_mb())
            except Exception:
                return

    def stop(self):
        self._stop.set()


def dir_size_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1e6


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 20000

    print(f"building {n:,}-page fixtures (first run only) ...")
    t0 = time.monotonic()
    a, b = make_fixtures.make_large_pair(n)
    print(f"  fixtures ready in {time.monotonic() - t0:.1f}s")
    print(f"  A: {os.path.getsize(a) / 1e6:7.1f} MB")
    print(f"  B: {os.path.getsize(b) / 1e6:7.1f} MB")

    phase_times: dict[str, float] = {}
    state = {"phase": None, "t": time.monotonic(), "last_print": 0.0}

    def on_progress(p):
        now = time.monotonic()
        if p.phase != state["phase"]:
            if state["phase"]:
                phase_times[state["phase"]] = now - state["t"]
            state["phase"] = p.phase
            state["t"] = now
            print(f"\n  {PHASE_LABELS.get(p.phase, p.phase)}")
        if now - state["last_print"] > 1.5 and p.total:
            state["last_print"] = now
            eta = f"  eta {p.eta_s:5.0f}s" if p.eta_s else ""
            print(f"    {p.fraction * 100:5.1f}%  {p.message}"
                  f"  ({p.rate:,.0f}/s){eta}")

    mem = PeakMemory()
    mem.start()

    eng = ComparisonEngine(
        a, b,
        settings=CompareSettings(compare_images=True, compare_formatting=False),
        runtime=RuntimeSettings(chunk_pages=64),
        progress_cb=on_progress)

    t0 = time.monotonic()
    summary = eng.run()
    total = time.monotonic() - t0
    if state["phase"]:
        phase_times[state["phase"]] = time.monotonic() - state["t"]
    mem.stop()

    print("\n" + "=" * 62)
    print(f"  pages            {summary.pages_a:,} vs {summary.pages_b:,}")
    print(f"  workers          {eng.runtime.resolved_workers()}")
    print(f"  total wall time  {total:.1f}s")
    for phase, secs in phase_times.items():
        if secs > 0.05:
            print(f"    {PHASE_LABELS.get(phase, phase):<20} {secs:7.1f}s")
    print(f"  throughput       {(summary.pages_a + summary.pages_b) / total:,.0f}"
          f" pages/s")
    print(f"  peak RSS (all)   {mem.peak_mb:,.0f} MB"
          f"   parent {mem.peak_parent_mb:,.0f} MB")
    print(f"  session on disk  {dir_size_mb(eng.session_dir):,.0f} MB")
    print("-" * 62)
    print(f"  identical        {summary.identical:,}")
    print(f"  changed          {summary.changed:,}")
    print(f"  pages added      {summary.added_pages:,}")
    print(f"  pages removed    {summary.removed_pages:,}")
    print(f"  alignment        {eng.align_stats}")
    print(f"  change counts    {summary.counts}")

    # Re-diff with an ignore rule: proves settings changes skip PDF parsing.
    print("\n  re-diff with footer ignore (must not re-parse) ...")
    t0 = time.monotonic()
    eng.progress_cb = None
    again = eng.rediff(CompareSettings(footer_ignore_pt=40.0))
    print(f"    {time.monotonic() - t0:.1f}s  -> changed={again.changed:,}")

    eng.close()
    print("=" * 62)


if __name__ == "__main__":
    main()
