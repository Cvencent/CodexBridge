from __future__ import annotations

import argparse
import os
import threading
import time
from pathlib import Path

from codexbridge.admin_api import AdminApi
from codexbridge.codex.collector import records_from_snapshot, run_codex_collector
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


class _CodexState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._enabled = True
        self._state = "starting"
        self._detail = "正在启动 Codex 采集"
        self._last_snapshot_epoch = 0

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = enabled

    def report(self, state: str, detail: str) -> None:
        with self._lock:
            self._state = state
            self._detail = detail

    def record_snapshot(self) -> None:
        with self._lock:
            self._last_snapshot_epoch = int(time.time())

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "enabled": self._enabled,
                "collector_state": self._state,
                "collector_detail": self._detail,
                "last_snapshot_epoch": self._last_snapshot_epoch,
            }


def _codex_working_directory() -> Path:
    configured = os.environ.get("CODEXBRIDGE_CODEX_CWD")
    if configured:
        return Path(configured).expanduser()
    return Path.home()


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
    codex_state = _CodexState()

    def publish_codex(records: list) -> None:
        codex_state.record_snapshot()
        for record in records:
            hub.publish(record)

    admin = AdminApi(
        hub,
        registry,
        on_device_removed=manager.remove,
        codex_status=codex_state.snapshot,
    )
    admin.start()
    stop = threading.Event()
    codex_stop = threading.Event()
    codex_thread = threading.Thread(
        target=run_codex_collector,
        kwargs={
            "runtime_folder": data_dir,
            "cwd": _codex_working_directory(),
            "interval": 5.0,
            "stop": codex_stop,
            "publish": publish_codex,
            "codex_command": os.environ.get("CODEXBRIDGE_CODEX_COMMAND"),
            "report": codex_state.report,
            "enabled": lambda: codex_state.snapshot()["enabled"],
        },
        daemon=True,
    )
    codex_thread.start()
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

            tray.run(hub, registry, manager)
    except KeyboardInterrupt:
        pass
    finally:
        codex_stop.set()
        stop.set()
        manager.stop()
        admin.stop()
        codex_thread.join(timeout=1.0)
        discovery_thread.join(timeout=1.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
