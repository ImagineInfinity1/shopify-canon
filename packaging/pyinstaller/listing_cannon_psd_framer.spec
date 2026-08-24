# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_all


repo_root = Path(SPECPATH).parents[1]
local_worker = repo_root / "local_worker"

datas = []
icon_path = local_worker / "listing_cannon.ico"
if icon_path.exists():
    datas.append((str(icon_path), "."))

numpy_datas, numpy_binaries, numpy_hiddenimports = collect_all("numpy")
psd_datas, psd_binaries, psd_hiddenimports = collect_all("psd_tools")
photoshop_datas, photoshop_binaries, photoshop_hiddenimports = collect_all("photoshopapi")
datas += numpy_datas + psd_datas + photoshop_datas
binaries = numpy_binaries + psd_binaries + photoshop_binaries
hiddenimports = [
    "PIL._tkinter_finder",
    "tkinterdnd2",
    "numpy",
    "numpy.core",
    "numpy.core.multiarray",
    "photoshopapi",
    "psd_tools",
] + numpy_hiddenimports + psd_hiddenimports + photoshop_hiddenimports

a = Analysis(
    [str(local_worker / "listing_cannon_desktop.py")],
    pathex=[str(repo_root), str(local_worker)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "Flask",
        "Flask_Login",
        "Flask_SQLAlchemy",
        "gunicorn",
        "psycopg2",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Listing Cannon PSD Framer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="Listing Cannon PSD Framer",
)
