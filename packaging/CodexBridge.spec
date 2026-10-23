# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import collect_all


ROOT = Path(SPECPATH).resolve().parent

datas = [
    (str(ROOT / "codexbridge" / "windows_speech_recognize.ps1"), "codexbridge"),
]

# Offline Vosk speech is loaded through cffi at runtime, so PyInstaller's static
# analysis cannot discover libvosk.dll or its MinGW runtime companions. Without
# this the frozen build silently falls back to Windows System.Speech even when
# the offline model is installed.
vosk_datas, vosk_binaries, vosk_hiddenimports = collect_all("vosk")
datas += vosk_datas

hiddenimports = [
    "pystray",
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
] + list(vosk_hiddenimports)

a = Analysis(
    [str(ROOT / "codexbridge" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=list(vosk_binaries),
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "IPython",
        "PyQt5",
        "PySide2",
        "PySide6",
        "aiohttp",
        "bleak",
        "esptool",
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
        "serial",
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
    a.binaries,
    a.datas,
    [],
    name="CodexBridge",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "codexbridge" / "assets" / "codexbridge.ico"),
)
