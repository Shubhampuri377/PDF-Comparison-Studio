# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for PDF Comparison Studio.

    pyinstaller app.spec

Two deliberate differences from the old main.spec:

* ``console=False`` — the previous build shipped with a console window that
  opened behind the GUI on every launch.
* No Poppler. PyMuPDF rasterises pages now, so roughly 100 MB of vendored
  binaries left the bundle. Qt adds back ~50 MB, so the net result is smaller
  than the 145 MB it replaced.
"""
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

project_dir = os.path.abspath(".")

# Bundled so the running app can set its window/taskbar icon from the same
# file the executable itself is stamped with.
datas = [("assets/icon.ico", "assets")]
binaries = []
hiddenimports = [
    # Spawned workers import these by name in the child process, so they must
    # survive the dependency scan even though nothing imports them directly
    # at module scope in the frozen entry point.
    "pdfdiff.extract",
    "pdfdiff.diffcore",
    "pdfdiff.store",
    "pdfdiff.models",
    "pdfdiff.ignore",
    "multiprocessing.pool",
    "multiprocessing.spawn",
]

for package in ("pymupdf", "PIL"):
    pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# Qt: pull the modules actually used and leave the rest out.
for module in ("PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"):
    hiddenimports += collect_submodules(module)

excludes = [
    # Trimming what the environment happens to have installed but the app
    # never touches keeps the bundle honest.
    "PyQt6", "PyQt5", "tkinter", "matplotlib", "scipy", "pandas", "numpy",
    "streamlit", "altair", "pyarrow", "nibabel", "nipype", "networkx",
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
    "PySide6.QtMultimedia", "PySide6.QtQuick", "PySide6.QtQml",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtBluetooth",
    "PySide6.QtNetworkAuth", "PySide6.QtPositioning", "PySide6.QtSql",
    "PySide6.QtTest", "PySide6.QtDesigner",
]

a = Analysis(
    ["app.py"],
    pathex=[project_dir],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PDFComparisonStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=["Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll"],
    icon="assets/icon.ico",
    console=False,          # GUI app: no stray console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
