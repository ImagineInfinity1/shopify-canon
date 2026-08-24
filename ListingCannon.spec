# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Listing Cannon desktop PSD-framing worker.

Builds a one-folder Windows bundle (dist/ListingCannon/ListingCannon.exe).
One-folder is used deliberately: photoshopapi ships native DLLs that are far
more reliable extracted alongside the exe than unpacked from a one-file stub.
The Inno Setup script (installer/ListingCannon.iss) wraps this folder into a
proper Setup.exe, so the end user never sees the folder anyway.

Build:  python -m PyInstaller --noconfirm ListingCannon.spec
"""
import os
from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

REPO_ROOT = os.path.abspath(os.getcwd())
WORKER_DIR = os.path.join(REPO_ROOT, "local_worker")
ICON = os.path.join(WORKER_DIR, "listing_cannon.ico")

# Bundle the app icon at the bundle root; the app looks for it at _MEIPASS.
datas = [(ICON, ".")]
binaries = []
hiddenimports = [
    "smart_mockup_engine",
    "listing_cannon_local_worker",
    "tkinterdnd2",
]

# Fully collect the native/data-carrying packages. numpy 2.x in particular
# needs everything: its `numpy.core` compat shims are imported dynamically by
# psd_tools/photoshopapi, and a plain hiddenimport misses them (runtime error
# "No module named 'numpy.core.multiarray'").
for _pkg in ("numpy", "photoshopapi", "psd_tools", "tkinterdnd2"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# Explicit numpy 2.x compat submodules that are resolved lazily.
hiddenimports += [
    "numpy.core.multiarray",
    "numpy.core._multiarray_umath",
    "numpy._core.multiarray",
    "numpy._core._multiarray_umath",
]

a = Analysis(
    [os.path.join(WORKER_DIR, "listing_cannon_desktop.py")],
    pathex=[REPO_ROOT, WORKER_DIR],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["flask", "gunicorn", "sqlalchemy", "psycopg2", "google", "google_genai"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ListingCannon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ListingCannon",
)
