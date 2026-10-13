from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from codexbridge.admin_api import AdminApi
from codexbridge.codex.collector import records_from_snapshot, run_codex_collector
from codexbridge.devices.discovery import discover
from codexbridge.devices.manager import DeviceManager
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.management_process import (
    ManagementProcess,
    is_esptool_argv,
    run_esptool,
    run_management_child,
)
from codexbridge.speech import SpeechService


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


def _restart_command() -> list[str]:
    executable = str(Path(sys.executable).resolve())
    if getattr(sys, "frozen", False):
        return [executable]
    return [executable, "-m", "codexbridge"]


def _launch_restarted_process(
    data_dir: Path,
    popen=None,
) -> None:
    launcher = subprocess.Popen if popen is None else popen
    project_root = Path(__file__).resolve().parents[1]
    cwd = data_dir if getattr(sys, "frozen", False) else project_root
    launcher(
        _restart_command(),
        cwd=str(cwd),
        close_fds=True,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if is_esptool_argv(arguments):
        return run_esptool(arguments)

    parser = argparse.ArgumentParser(prog="codexbridge")
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="run the host and loopback admin API without a tray icon",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help="open the local settings page and exit",
    )
    parser.add_argument(
        "--management-child",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--no-management",
        action="store_true",
        help="run without the Dotii management child",
    )
    parser.add_argument(
        "--startup",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(arguments)

    if args.management_child:
        return run_management_child(["--no-browser"])

    if args.settings:
        import webbrowser

        webbrowser.open("http://127.0.0.1:8788/settings")
        return 0

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    registry = DeviceRegistry(data_dir / "devices.json")
    registry.load()
    _migrate_legacy(registry)

    speech = SpeechService(output_dir=data_dir / "recordings")
    hub = BridgeHub(registry, speech=speech)
    manager = DeviceManager(registry, hub)
    codex_state = _CodexState()

    def publish_codex(records: list) -> None:
        codex_state.record_snapshot()
        for record in records:
            hub.publish(record)

    def open_logs() -> None:
        os.startfile(data_dir)

    restart_requested = threading.Event()

    def restart_process() -> None:
        restart_requested.set()

    admin = AdminApi(
        hub,
        registry,
        on_device_removed=manager.remove,
        codex_status=codex_state.snapshot,
        open_logs=open_logs,
    )
    admin.start()
    management = None
    if not args.no_management:
        management = ManagementProcess(
            Path(sys.executable),
            bool(getattr(sys, "frozen", False)),
        )
        management.start()
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

    def poll_speech() -> None:
        while not stop.wait(0.25):
            hub.poll_speech()

    speech_thread = threading.Thread(target=poll_speech, daemon=True)
    speech_thread.start()

    management_stop = threading.Event()

    def poll_management() -> None:
        while not management_stop.wait(1.0):
            if management is None:
                return
            management.poll()
            if management.failed:
                return

    management_thread = None
    if management is not None:
        management_thread = threading.Thread(target=poll_management, daemon=True)
        management_thread.start()

    try:
        if args.no_tray:
            print(f"CodexBridge admin API: {admin.url}", flush=True)
            while not stop.wait(1.0):
                if management is not None and management.failed:
                    break
        else:
            from codexbridge import tray

            tray.run(
                hub,
                registry,
                manager,
                on_restart=restart_process,
                management_url=management.url if management is not None else None,
                management_failed=lambda: bool(management and management.failed),
            )
    except KeyboardInterrupt:
        pass
    finally:
        management_stop.set()
        if management_thread is not None:
            management_thread.join(timeout=1.0)
        if management is not None:
            management.stop()
        codex_stop.set()
        stop.set()
        manager.stop()
        speech.close()
        admin.stop()
        speech_thread.join(timeout=1.0)
        codex_thread.join(timeout=1.0)
        discovery_thread.join(timeout=1.0)
    if restart_requested.is_set():
        _launch_restarted_process(data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
