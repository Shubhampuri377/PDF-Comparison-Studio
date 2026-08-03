"""PDF Comparison Studio — application entry point.

    python app.py               launch the GUI
    python app.py --selftest    headless check that this build works

``freeze_support`` must run before anything spawns a process: on Windows the
child re-imports this module, and without the guard a frozen build would launch
a new window per worker instead of a worker.
"""
from __future__ import annotations

import multiprocessing
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def selftest() -> int:
    """Run a real comparison headlessly and report whether it worked.

    Exists mainly for the packaged executable. A PyInstaller build can start
    up perfectly and still fail the moment it spawns a worker -- a missing
    hidden import, or a ``freeze_support`` mistake that relaunches the whole
    application per worker. This exercises that path deliberately, with enough
    pages to push the engine past its in-process threshold, so a broken build
    is caught before a customer sees it.
    """
    import shutil
    import tempfile
    import time

    print("PDF Comparison Studio — self test")
    print(f"  frozen        {getattr(sys, 'frozen', False)}")
    print(f"  executable    {sys.executable}")

    work = tempfile.mkdtemp(prefix="pdfdiff_selftest_")
    try:
        try:
            import pymupdf as fitz
        except ImportError:
            import fitz
        print(f"  PyMuPDF       {fitz.__doc__.strip().splitlines()[0]}")

        # Two 40-page documents: 80 pages total, above the threshold where the
        # engine switches from in-process to a worker pool.
        paths = []
        for tag, edit in (("a", False), ("b", True)):
            doc = fitz.open()
            for i in range(40):
                page = doc.new_page(width=595, height=842)
                page.insert_text((60, 60), f"Self test document, page {i + 1}",
                                 fontsize=13)
                for line in range(18):
                    text = f"Line {line} of page {i} lorem ipsum dolor sit amet"
                    if edit and i == 7 and line == 3:
                        text = "THIS LINE DIFFERS BETWEEN THE TWO DOCUMENTS"
                    page.insert_text((60, 100 + line * 18), text, fontsize=10)
            path = os.path.join(work, f"selftest_{tag}.pdf")
            doc.save(path)
            doc.close()
            paths.append(path)

        from pdfdiff.engine import ComparisonEngine
        from pdfdiff.models import CompareSettings, RuntimeSettings

        phases = []
        engine = ComparisonEngine(
            paths[0], paths[1],
            settings=CompareSettings(),
            runtime=RuntimeSettings(workers=2, chunk_pages=16),
            progress_cb=lambda p: phases.append(p.phase))

        started = time.monotonic()
        summary = engine.run()
        elapsed = time.monotonic() - started
        workers = engine.runtime.resolved_workers()
        engine.close()

        print(f"  workers       {workers}")
        print(f"  phases seen   {', '.join(dict.fromkeys(phases))}")
        print(f"  pages         {summary.total_pairs}")
        print(f"  identical     {summary.identical}")
        print(f"  changed       {summary.changed}")
        print(f"  elapsed       {elapsed:.1f}s")

        problems = []
        if summary.total_pairs != 40:
            problems.append(f"expected 40 pages, got {summary.total_pairs}")
        if summary.changed != 1:
            problems.append(f"expected 1 changed page, got {summary.changed}")
        if "ingest" not in phases:
            problems.append("ingest phase never ran")

        from pdfdiff.licensing import PUBLIC_KEY_B64, current_status
        status = current_status()
        print(f"  public key    {PUBLIC_KEY_B64}")
        print(f"  licence       {status.reason}")

        print()
        if problems:
            for p in problems:
                print(f"  FAILED: {p}")
            return 1
        print("  SELF TEST PASSED — this build parses, forks workers and "
              "compares correctly.")
        return 0
    except Exception as exc:
        import traceback

        print()
        print(f"  SELF TEST FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main() -> int:
    multiprocessing.freeze_support()

    if "--selftest" in sys.argv:
        return selftest()

    from pdfdiff.ui.main import run

    return run()


if __name__ == "__main__":
    sys.exit(main())
