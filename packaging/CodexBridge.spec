# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


ROOT = Path(SPECPATH).resolve().parent


a = Analysis(
    [str(ROOT / "codexbridge" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "codexbridge" / "windows_speech_recognize.ps1"), "codexbridge"),
    ],
    hiddenimports=[
        "pystray",
        "pystray._win32",
        "PIL.Image",
        "PIL.ImageDraw",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "PyQt5",
        "PySide2",
        "PySide6",
        "aiohttp",
        "cryptography",
        "fsspec",
        "httpx",
        "ipykernel",
        "jupyter",
        "jupyter_client",
        "lxml",
        "matplotlib",
        "nacl",
        "numpy",
        "openpyxl",
        "pandas",
        "pyarrow",
        "pydantic",
        "scipy",
        "sqlalchemy",
        "tkinter",
        "tornado",
        "zmq",
    ],
    noarchive=False,
    optimize=0,
    module_collection_mode={},
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="CodexBridge",
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
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="CodexBridge",
)