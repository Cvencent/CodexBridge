from __future__ import annotations

import argparse
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from codexbridge.admin_api import AdminApi
from codexbridge.codex.app_server import AppServerClient, AppServerError, _resolve_codex_command
from codexbridge.codex.catalog import CodexCatalogService
from codexbridge.codex.collector import records_from_snapshot, run_codex_collector
from codexbridge.devices.discovery import discover
from codexbridge.devices.manager import DeviceManager
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.hub import BridgeHub
from codexbridge.companion import (
    CompanionApi,
    CompanionDiscovery,
    CompanionService,
    CompanionStore,
)
from codexbridge.ports import (
    ADMIN_PORT,
    build_port_statuses,
    validate_companion_port,
    validate_discovery_port,
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
        # Devices sit on the network persistently, so re-scanning every few
        # seconds only adds radio wake-ups. 15 s still reconnects after a
        # device reboot well within the session timeout.
        stop.wait(15.0)


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


class _CodexSourceHolder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._source = None
        self._fallback = None
        self._fallback_command = None
        self._fallback_cwd = None
        self._fallback_runtime = None

    def configure(self, command: str | None, cwd: Path, runtime_folder: Path) -> None:
        with self._lock:
            self._fallback_command = command
            self._fallback_cwd = cwd
            self._fallback_runtime = runtime_folder

    def set(self, source) -> None:
        with self._lock:
            self._source = source
            if source is None:
                fallback = self._fallback
                self._fallback = None
            else:
                fallback = None
        if fallback is not None:
            fallback.close()

    def request(self, method: str, params: dict) -> object:
        with self._lock:
            source = self._source
            if source is not None:
                return source.client.request(method, params)
            fallback = self._fallback
            if fallback is None:
                if self._fallback_cwd is None:
                    raise AppServerError("Codex App Server 尚未连接")
                command = _resolve_codex_command(
                    self._fallback_command,
                    self._fallback_runtime,
                )
                fallback = AppServerClient(command, self._fallback_cwd)
                fallback.start()
                self._fallback = fallback
            return fallback.request(method, params)

    def close(self) -> None:
        with self._lock:
            primary = self._source
            fallback = self._fallback
            self._source = None
            self._fallback = None
        if fallback is not None:
            fallback.close()
        if primary is not None and hasattr(primary, "close"):
            primary.close()


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


def _apply_companion_action(
    payload: dict,
    *,
    companion,
    discovery,
    store,
    status,
    set_enabled=None,
) -> dict:
    action = str(payload.get("action") or "")
    if action == "new_code":
        return {
            "pairing": store.pairing_code(regenerate=True),
            "companion": status(),
        }
    if action == "remove_phone":
        removed = store.remove_phone(str(payload.get("phone_id") or ""))
        return {"removed": removed, "companion": status()}
    if action == "set_port":
        kind = str(payload.get("kind") or "companion")
        if kind == "discovery":
            port = validate_discovery_port(payload.get("port"))
            discovery.set_port(port)
        elif kind == "companion":
            port = validate_companion_port(payload.get("port"))
            companion.set_port(port)
        else:
            raise ValueError("port_kind")
        return {"companion": status()}
    if "enabled" in payload:
        value = payload.get("enabled")
        if not isinstance(value, bool):
            raise ValueError("enabled")
        if set_enabled is None:
            raise ValueError("enabled")
        set_enabled(value)
        return {"companion": status()}
    raise ValueError("action")


def main(argv: list[str] | None = None) -> int:
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
        "--startup",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

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
    source_holder = _CodexSourceHolder()
    codex_cwd = _codex_working_directory()
    codex_command = os.environ.get("CODEXBRIDGE_CODEX_COMMAND")
    source_holder.configure(codex_command, codex_cwd, data_dir)
    codex_catalog = CodexCatalogService(
        source_holder.request,
        workspace=codex_cwd,
    )
    hub = BridgeHub(registry, speech=speech, codex_catalog=codex_catalog)
    manager = DeviceManager(registry, hub)
    codex_state = _CodexState()

    companion_store = CompanionStore(data_dir / "companion.json")
    companion_store.load()
    companion_service = CompanionService(data_dir, codex_cwd, codex_command)
    hub.commands = companion_service.commands
    companion = CompanionApi(companion_service, companion_store, logger=print)
    companion_discovery = CompanionDiscovery(
        companion_store,
        port=companion_store.discovery_port,
        logger=print,
    )

    def companion_status() -> dict:
        payload = companion.status_payload()
        payload["ports"] = build_port_statuses(
            admin_port=ADMIN_PORT,
            companion_port=int(payload.get("port") or companion.store.port),
            discovery_port=int(
                payload.get("discovery_port") or companion_store.discovery_port
            ),
            companion_enabled=bool(payload.get("enabled")),
            companion_running=companion.running,
            discovery_running=companion_discovery.running,
            admin_running=True,
        )
        return payload

    def companion_set_enabled(enabled: bool) -> None:
        companion_store.set_enabled(enabled)
        if enabled:
            companion_service.start()
            companion.start()
            companion_discovery.start()
        else:
            companion_discovery.stop()
            companion.stop()
            companion_service.stop()

    def companion_action(payload: dict) -> dict:
        return _apply_companion_action(
            payload,
            companion=companion,
            discovery=companion_discovery,
            store=companion_store,
            status=companion_status,
            set_enabled=companion_set_enabled,
        )

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
        companion_status=companion_status,
        companion_action=companion_action,
    )
    admin.start()
    if companion_store.enabled:
        try:
            companion_service.start()
            companion.start()
            companion_discovery.start()
            print(f"CodexBridge companion API: {companion.url}", flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"CodexBridge companion API 启动失败：{error}", flush=True)
    stop = threading.Event()
    codex_stop = threading.Event()
    codex_thread = threading.Thread(
        target=run_codex_collector,
        kwargs={
            "runtime_folder": data_dir,
            "cwd": codex_cwd,
            "interval": 5.0,
            "stop": codex_stop,
            "publish": publish_codex,
            "codex_command": codex_command,
            "report": codex_state.report,
            "enabled": lambda: codex_state.snapshot()["enabled"],
            "on_source": source_holder.set,
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
            hub.poll_events()

    speech_thread = threading.Thread(target=poll_speech, daemon=True)
    speech_thread.start()

    try:
        if args.no_tray:
            print(f"CodexBridge admin API: {admin.url}", flush=True)
            while not stop.wait(1.0):
                pass
        else:
            from codexbridge import tray

            tray.run(
                hub,
                registry,
                manager,
                on_restart=restart_process,
            )
    except KeyboardInterrupt:
        pass
    finally:
        codex_stop.set()
        stop.set()
        # Let queued Codex work finish before the sessions go away, otherwise a
        # shutdown mid-request leaves devices with a half-delivered catalog.
        hub.wait_idle(timeout=2.0)
        manager.stop()
        speech.close()
        companion_discovery.stop()
        companion.stop()
        companion_service.stop()
        admin.stop()
        speech_thread.join(timeout=1.0)
        codex_thread.join(timeout=1.0)
        discovery_thread.join(timeout=1.0)
        # Close the fallback client only after the collector has stopped using
        # the source holder; closing it first can race an in-flight request.
        source_holder.close()
    if restart_requested.is_set():
        _launch_restarted_process(data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
