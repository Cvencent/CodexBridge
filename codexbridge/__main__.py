from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

from codexbridge.admin_api import AdminApi
from codexbridge.devices.discovery import discover
from codexbridge.devices.manager import DeviceManager
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub


def _data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA")
    base = Path(root) if root else Path.home() / ".local" / "share"
    return base / "CodexBridge"


def _migrate_legacy(registry: DeviceRegistry) -> None:
    root = os.environ.get("LOCALAPPDATA")
    if not root:
        return
    legacy = Path(root) / "FoloOS" / "bridge.json"
    if legacy.exists():
        registry.migrate_legacy(legacy)


def _discovery_loop(
    manager: DeviceManager,
    stop: threading.Event,
) -> None:
    while not stop.is_set():
        try:
            results = discover(timeout=1.0)
        except OSError:
            results = []
        manager.sync(results)
        stop.wait(5.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="codexbridge")
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="run the host and loopback admin API without a tray icon",
    )
    args = parser.parse_args(argv)

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    registry = DeviceRegistry(data_dir / "devices.json")
    registry.load()
    _migrate_legacy(registry)

    hub = BridgeHub(registry)
    manager = DeviceManager(registry, hub)
    admin = AdminApi(hub, registry)
    admin.start()
    stop = threading.Event()
    discovery_thread = threading.Thread(
        target=_discovery_loop,
        args=(manager, stop),
        daemon=True,
    )
    discovery_thread.start()

    try:
        if args.no_tray:
            print(f"CodexBridge admin API: {admin.url}", flush=True)
            while not stop.wait(1.0):
                pass
        else:
            from codexbridge import tray

            tray.run(hub, registry)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        manager.stop()
        admin.stop()
        discovery_thread.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
