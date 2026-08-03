"""pdfdiff - a scalable PDF comparison engine and desktop app.

The package is deliberately split so that everything under ``pdfdiff`` except
``pdfdiff.ui`` is import-safe inside multiprocessing workers (no Qt, no GUI
state).  Do not import ``pdfdiff.ui`` from any module that a worker touches.
"""

__version__ = "2.0.0"
APP_NAME = "PDF Comparison Studio"
