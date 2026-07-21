# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH).resolve()
datas = [
    (str(root / "vendor" / "UberWolfCli.exe"), "vendor"),
    (str(root / "vendor" / "uv.exe"), "vendor"),
    (str(root / "vendor" / "manifest.json"), "vendor"),
    (str(root / "vendor" / "licenses"), "vendor/licenses"),
    (str(root / "LICENSE"), "."),
    (str(root / "THIRD_PARTY_NOTICES.md"), "."),
]

a = Analysis(
    ["app.py"],
    pathex=[str(root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="WOLFLator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="WOLFLator",
)
