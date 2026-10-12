from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

from codexbridge.admin_api import AdminApi
from codexbridge.devices.discovery import discover
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
    registry: DeviceRegistry,
    hub: BridgeHub,
    stop: threading.Event,
) -> None:
    while not stop.is_set():
        try:
            results = discover(timeout=1.0)
        except OSError:
            results = []
        for result in results:
            record = registry.get(result.device_id)
            if record is None:
                record = registry.upsert(
                    result.device_id,
                    model=result.device,
                    name=result.device,
                    supported=result.capabilities,
                )
            else:
                registry.upsert(
                    result.device_id,
                    model=result.device,
                    name=record.name or result.device,
                    supported=result.capabilities,
                )
            registry.get(result.device_id).last_seen = time.time()
            registry.save()
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
    admin = AdminApi(hub, registry)
    admin.start()
    stop = threading.Event()
    discovery_thread = threading.Thread(
        target=_discovery_loop,
        args=(registry, hub, stop),
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
        admin.stop()
        discovery_thread.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
