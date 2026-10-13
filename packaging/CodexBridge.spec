# -*- mode: python ; coding: utf-8 -*-

import json
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files


ROOT = Path(SPECPATH).resolve().parent
FIRMWARE_ROOT = ROOT / "firmware"
FIRMWARE_MANIFEST = FIRMWARE_ROOT / "flasher_args.json"

if not FIRMWARE_MANIFEST.is_file():
    raise SystemExit(
        "缺少 Dotii 固件包：请先准备 firmware/flasher_args.json，"
        "或让 build_windows.ps1 从 -FirmwareRoot 复制。"
    )

payload = json.loads(FIRMWARE_MANIFEST.read_text(encoding="utf-8"))
flash_files = payload.get("flash_files")
if not isinstance(flash_files, dict) or not flash_files:
    raise SystemExit("firmware/flasher_args.json 缺少 flash_files。")

datas = [
    (str(ROOT / "codexbridge" / "windows_speech_recognize.ps1"), "codexbridge"),
    (str(FIRMWARE_MANIFEST), "firmware"),
]
datas.extend(
    collect_data_files(
        "codexbridge.management",
        includes=[
            "state.json",
            "assets/*",
            "web/*",
            "web/assets/*",
            "web/assets/expressions/*",
        ],
    )
)
for relative_name in flash_files.values():
    source = (FIRMWARE_ROOT / str(relative_name)).resolve()
    try:
        relative = source.relative_to(FIRMWARE_ROOT.resolve())
    except ValueError as error:
        raise SystemExit(f"固件文件越出目录：{relative_name}") from error
    if not source.is_file():
        raise SystemExit(f"缺少固件文件：{relative_name}")
    datas.append((str(source), str(Path("firmware") / relative.parent)))

hiddenimports = [
    "pystray",
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "bleak",
    "bleak.backends",
    "bleak.backends.bluezdbus",
    "bleak.backends.corebluetooth",
    "bleak.backends.winrt",
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "esptool",
    "esptool.targets",
    "esptool.targets.stub_flasher",
]

esptool_datas, esptool_binaries, esptool_hiddenimports = collect_all("esptool")
datas.extend(esptool_datas)
binaries = esptool_binaries
hiddenimports.extend(esptool_hiddenimports)

a = Analysis(
    [str(ROOT / "codexbridge" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
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
    icon=str(ROOT / "codexbridge" / "management" / "assets" / "dotii.ico"),
)
