from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from codexbridge.admin_api import AdminApi
from codexbridge.codex.app_server import (
    AppServerClient,
    AppServerError,
    CodexAppServerSource,
    _resolve_codex_command,
)
from codexbridge.codex.catalog import CodexCatalogService
from codexbridge.codex.collector import records_from_snapshot, run_codex_collector
from codexbridge.devices.discovery import (
    DISCOVERY_TIMEOUT_SECONDS,
    PresenceListener,
    discover,
)
from codexbridge.devices.manager import DeviceManager
from codexbridge.desktop_read import sync_desktop_read_state
from codexbridge.devices.registry import DeviceRegistry
from codexbridge.devices.session import CbdpDeviceSession
from codexbridge.devices.home_counters import HomeCounterSender
from codexbridge.hub import BridgeHub
from codexbridge.messages_store import MessageStore
from codexbridge.cbdp.events import EventSpine
from codexbridge.cbdp.gateway import CbdpGateway
from codexbridge.cbdp.http_transport import CbdpHttpBroker
from codexbridge.cbdp.leases import LeaseManager
from codexbridge.cbdp.projection import ProjectionStore
from codexbridge.cbdp.refs import ReferenceRegistry
from codexbridge.cbdp.subscriptions import SubscriptionManager
from codexbridge.read_state import ReadStateStore
from codexbridge.companion import (
    CompanionApi,
    CompanionDiscovery,
    CompanionService,
    CompanionStore,
    CompanionStoreError,
)
from codexbridge.console import emit
from codexbridge.net_gate import port_is_in_use
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


def _repair_uninstall_record() -> None:
    """Fix the Apps & features entry when an upgrade was cut short."""

    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    try:
        from codexbridge.uninstall_record import sync_uninstall_record

        repaired = sync_uninstall_record()
    except Exception:
        return
    for key in repaired:
        emit(f"已修正 Windows 卸载记录中的版本号：{key}")


def _discovery_loop(
    manager: DeviceManager,
    stop: threading.Event,
) -> None:
    while not stop.is_set():
        # A remembered endpoint is the fastest path, so dial it before spending
        # a broadcast round: a device that is already listening is connected in
        # milliseconds instead of after the next announcement window.
        manager.redial_paired()
        try:
            results = discover(timeout=DISCOVERY_TIMEOUT_SECONDS)
        except OSError:
            results = []
        manager.sync(results)
        # The device carries the low-latency path itself: it announces as soon
        # as its listener exists, so this broadcast is only a safety net for a
        # datagram that never arrived, a device being paired for the first time,
        # or an address that moved. Nothing here needs to be urgent, and a slow
        # beat keeps both radios quiet, which the home screen's temperature
        # depends on.
        if manager.online_count() and not manager.paired_count():
            # Every paired device is connected; announce only to catch a reboot.
            cadence = 15.0
        else:
            cadence = 5.0
        stop.wait(cadence)


class _CodexState:
    """Whether Codex is polled in the background while nobody is connected.

    This is a cadence knob, not a feature switch. Whenever a device or phone is
    connected the collector runs at full speed regardless of this value, so no
    client ever loses an update. With nothing connected:

    * on  -> keep the original fast poll;
    * off -> drop to a slow beacon, which is what turns the ~13% of one core
      this machine was burning while unattended into nearly zero.

    The choice persists and defaults to off, because an idle machine should not
    be kept warm for a client that is not there.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._path = Path(path) if path is not None else None
        self._enabled = False
        self._state = "idle"
        self._detail = "空闲时降低 Codex 采集频率"
        self._last_snapshot_epoch = 0
        if self._path is not None:
            self._enabled = self._load_enabled()

    def _load_enabled(self) -> bool:
        assert self._path is not None
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        value = payload.get("enabled")
        return value if isinstance(value, bool) else False

    def _save_enabled_locked(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            temporary.write_text(
                json.dumps({"enabled": self._enabled}, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        except OSError:
            # A settings file that cannot be written must not block the switch.
            pass

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self._enabled = bool(enabled)
            self._state = "starting" if self._enabled else "idle"
            self._detail = (
                "空闲时也保持 Codex 全速采集"
                if self._enabled
                else "空闲时降低 Codex 采集频率"
            )
            self._save_enabled_locked()

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
        self._fallback_client = None
        self._owns_fallback_client = True

    def configure(
        self,
        command: str | None,
        cwd: Path,
        runtime_folder: Path,
        *,
        fallback_client=None,
        owns_fallback: bool = True,
    ) -> None:
        with self._lock:
            self._fallback_command = command
            self._fallback_cwd = cwd
            self._fallback_runtime = runtime_folder
            self._fallback_client = fallback_client
            self._owns_fallback_client = bool(owns_fallback)

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
        # Deliberately NOT gated on the background-collection switch: a device
        # that explicitly opens the task list must still get an answer. The
        # switch only stops the poll loop, never on-demand reads.
        with self._lock:
            source = self._source
            fallback = self._fallback or self._fallback_client
            if source is None and fallback is None:
                if self._fallback_cwd is None:
                    raise AppServerError("Codex App Server 尚未连接")
                command = _resolve_codex_command(
                    self._fallback_command,
                    self._fallback_runtime,
                )
                fallback = AppServerClient(command, self._fallback_cwd)
                fallback.start()
                self._fallback = fallback
        if source is not None:
            return source.client.request(method, params)
        assert fallback is not None
        if fallback is not self._fallback:
            fallback.start()
        return fallback.request(method, params)

    def close(self) -> None:
        with self._lock:
            primary = self._source
            fallback = self._fallback
            fallback_client = self._fallback_client if self._owns_fallback_client else None
            self._source = None
            self._fallback = None
            self._fallback_client = None
        if fallback is not None:
            fallback.close()
        if fallback_client is not None:
            fallback_client.close()
        if primary is not None and hasattr(primary, "close"):
            primary.close()


def _codex_working_directory() -> Path:
    configured = os.environ.get("CODEXBRIDGE_CODEX_CWD")
    if configured:
        return Path(configured).expanduser()
    return Path.home()


def _sync_home_counter_target(
    home_counters: HomeCounterSender,
    registry: DeviceRegistry,
    device_id: str,
    host: str | None = None,
) -> bool:
    """Register or drop a device as a UDP home-counter target.

    The counter channel is part of the device protocol, so only an approved
    device that actually holds a pairing token may receive it. A pending,
    rejected or revoked device that merely announces itself on UDP must never
    see the host's Codex counters. Returns True when the device is registered.
    """

    record = registry.get(device_id)
    endpoint = host or (getattr(record, "host", None) if record is not None else None)
    if (
        record is not None
        and record.registration_state == "approved"
        and record.token
        and endpoint
    ):
        home_counters.register(device_id, endpoint)
        return True
    home_counters.unregister(device_id)
    return False


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
    set_show_intermediate_events=None,
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
    if "show_intermediate_events" in payload:
        value = payload.get("show_intermediate_events")
        if not isinstance(value, bool):
            raise ValueError("show_intermediate_events")
        if set_show_intermediate_events is None:
            raise ValueError("show_intermediate_events")
        set_show_intermediate_events(value)
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


# Two CodexBridge processes on one machine is never a good idea: each dials the
# same devices, and a device holds exactly one TCP session, so the newer dial
# tears the older one down mid-transfer. That showed up as a recording that died
# with "connection aborted" a second after it started, and as a Codex page that
# flipped to an error the moment the user pressed the talk key. Both the
# installed build and a source checkout can be open at the same time, so the
# guard asks the running peer to identify itself rather than trusting its own
# build. The admin API binds with SO_REUSEADDR, which on Windows lets a second
# process bind the same port silently, so a lock alone is not enough.
_INSTANCE_LOCK: object | None = None
# Normal Codex collection cadence, used whenever somebody is connected (or the
# user turned on always-on background collection).
COLLECT_SECONDS = 5.0
# Cadence while nothing is connected and background collection is off. This is
# a slow beacon, not a stop: the store still refreshes, so a device that shows
# up mid-cycle finds recent data and never an empty list.
IDLE_COLLECT_SECONDS = 60.0
_INSTANCE_LOCK_NAME = "Local\\CodexBridgeSingleton"


def _another_instance_is_running(admin_port: int = ADMIN_PORT) -> bool:
    """True when a CodexBridge is already answering its admin API."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{admin_port}/api/admin/status", timeout=1.5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def _acquire_instance_lock(admin_port: int = ADMIN_PORT) -> bool:
    """True when this process may run; False when another instance already holds it."""
    global _INSTANCE_LOCK
    if _another_instance_is_running(admin_port):
        return False
    if sys.platform == "win32":
        import ctypes
        import ctypes.wintypes

        error_already_exists = 183
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.wintypes.HANDLE
        handle = kernel32.CreateMutexW(None, False, _INSTANCE_LOCK_NAME)
        if not handle:
            # Cannot create the mutex; do not block a real user over a mystery.
            return True
        if ctypes.get_last_error() == error_already_exists:
            kernel32.CloseHandle(ctypes.wintypes.HANDLE(handle))
            return False
        _INSTANCE_LOCK = handle
    return True


def _release_instance_lock() -> None:
    global _INSTANCE_LOCK
    handle = _INSTANCE_LOCK
    _INSTANCE_LOCK = None
    if handle is None or sys.platform != "win32":
        return
    try:
        import ctypes
        import ctypes.wintypes

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.ReleaseMutex(ctypes.wintypes.HANDLE(handle))
        kernel32.CloseHandle(ctypes.wintypes.HANDLE(handle))
    except Exception:
        pass


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
        # The settings page talks to whichever instance is already running, so
        # this path must stay allowed while the lock is held.
        import webbrowser

        webbrowser.open("http://127.0.0.1:8788/settings")
        return 0

    if not _acquire_instance_lock():
        print(
            "已有一个 CodexBridge 在运行，本次启动退出。"
            "请先退出旧实例再重新打开。"
        )
        return 0

    _repair_uninstall_record()

    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    registry = DeviceRegistry(data_dir / "devices.json")
    registry.load()
    _migrate_legacy(registry)

    speech = SpeechService(output_dir=data_dir / "recordings")
    # Created before the Codex plumbing because both the collector and the
    # device catalog gate on it: switching collection off has to stop every
    # path that could restart the private app-server.
    codex_state = _CodexState(data_dir / "codex-collection.json")
    source_holder = _CodexSourceHolder()
    codex_cwd = _codex_working_directory()
    codex_command = os.environ.get("CODEXBRIDGE_CODEX_COMMAND")
    # Prefer the desktop's own app-server over spawning a second one: a private
    # stdio app-server keeps its own background watchers alive for the whole
    # session, and the desktop proxy is also what makes live desktop turns
    # visible to the collector.
    shared_app_server = AppServerClient(
        _resolve_codex_command(codex_command, data_dir),
        codex_cwd,
        prefer_shared=True,
    )
    source_holder.configure(
        codex_command,
        codex_cwd,
        data_dir,
        fallback_client=shared_app_server,
        owns_fallback=False,
    )
    codex_catalog = CodexCatalogService(
        source_holder.request,
        workspace=codex_cwd,
    )
    read_state = ReadStateStore(data_dir / "read-state.json")
    event_spine = EventSpine(data_dir / "cbdp-events.sqlite3", logger=print)
    projection = ProjectionStore(data_dir / "cbdp-projection.sqlite3")
    references = ReferenceRegistry(data_dir / "cbdp-refs.sqlite3")
    subscriptions = SubscriptionManager(event_spine)
    leases = LeaseManager()
    messages = MessageStore(path=data_dir / "messages.json")
    hub = BridgeHub(
        registry,
        store=messages,
        speech=speech,
        codex_catalog=codex_catalog,
        read_state=read_state,
        event_spine=event_spine,
        projection=projection,
        subscriptions=subscriptions,
        references=references,
    )

    project_service: dict[str, Any] = {}

    def project_lookup(project_id: str):
        """Resolve a device's project reference once the service exists."""

        service = project_service.get("service")
        if service is None:
            return None
        return service.project_cwd(project_id)

    def cbdp_gateway_factory(device_id, record):
        return CbdpGateway(
            commands=lambda: hub.commands,
            event_spine=event_spine,
            subscriptions=subscriptions,
            leases=leases,
            references=references,
            speech=speech,
            active_thread=hub.followed_thread,
            active_state=hub.active_state,
            set_active_state=hub.set_active_state,
            project_cwd=lambda project_id: project_lookup(project_id),
            defer=lambda device_id, callback: hub.defer_blocking_event(
                device_id,
                {"type": "cbdp_deferred", "callback": callback},
            ),
            pairing_token=lambda target: (
                candidate.token
                if (candidate := registry.get(target)) is not None
                else ""
            ),
            capability_policy=lambda target: hub.capabilities_for(target),
            transport_allowed=lambda target: (
                (1 << 64) - 1
                if (candidate := registry.get(target)) is not None
                else 0
            ),
        )

    manager = DeviceManager(
        registry,
        hub,
        cbdp_session_factory=CbdpDeviceSession,
        cbdp_gateway_factory=cbdp_gateway_factory,
    )

    companion_store = CompanionStore(data_dir / "companion.json", registry=registry)
    try:
        companion_store.load()
    except CompanionStoreError as error:
        print(f"手机配对存储读取失败：{error}")
        raise
    if companion_store.load_warning:
        print(f"手机配对存储已恢复：{companion_store.load_warning}")
    companion_service = CompanionService(
        data_dir,
        codex_cwd,
        codex_command,
        client_factory=lambda command, cwd: shared_app_server,
        creation_client_factory=lambda: AppServerClient(
            _resolve_codex_command(codex_command, data_dir),
            codex_cwd,
            prefer_shared=False,
        ),
        close_client_on_stop=False,
        read_state=read_state,
        show_intermediate_events=lambda: companion_store.show_intermediate_events,
        codex_enabled=lambda: codex_state.snapshot()["enabled"],
        demand=None,  # filled in below, once the hub exists
    )
    project_service["service"] = companion_service
    hub.commands = companion_service.commands
    companion_service.commands.selected_project_cwd = (
        codex_catalog.selected_project_cwd
    )

    def watcher_demand() -> bool:
        """True when somebody is relying on live Codex updates.

        Two things count: a device with an open TCP session (it renders task
        state continuously), or a phone holding the companion event stream
        open. Both resume the full-speed poll the moment they appear, so the
        optimisation is invisible to any client that is actually connected.
        """
        try:
            if hub.online_device_ids():
                return True
        except (OSError, RuntimeError, ValueError):
            return True
        return companion_service.has_event_subscribers()

    companion_service._demand = watcher_demand
    # Approval changes must also reach CBDP panels, not just the legacy JSON
    # event stream. Give the service the same spine and refs the device path
    # uses so it can append approval.* events.
    companion_service._cbdp_event_spine = event_spine
    companion_service._cbdp_references = references
    # A read made on the hardware also retracts the phone notification badge.
    hub.companion_events = companion_service
    cbdp_http = CbdpHttpBroker(
        commands=lambda: companion_service.commands,
        capability_for=lambda phone_id: (
            companion_store.get_phone(phone_id).capabilities()
            if companion_store.get_phone(phone_id)
            else set()
        ),
        hub=hub,
        references=references,
        active_state=hub.active_state,
        set_active_state=hub.set_active_state,
        event_spine=event_spine,
        subscriptions=subscriptions,
    )
    companion = CompanionApi(
        companion_service,
        companion_store,
        logger=print,
        cbdp_broker=cbdp_http,
    )
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

    def codex_action(payload: dict) -> dict:
        if "enabled" not in payload:
            raise ValueError("enabled_required")
        value = payload.get("enabled")
        if not isinstance(value, bool):
            raise ValueError("enabled_invalid")
        codex_state.set_enabled(value)
        # Nothing is shut down here. The switch only changes how often the
        # collector polls when no client is connected; the app-server stays
        # alive (an idle one measured 0.00% of one core) so a device or phone
        # that shows up still gets an immediate, complete task list.
        return {"codex": codex_state.snapshot()}

    def companion_action(payload: dict) -> dict:
        return _apply_companion_action(
            payload,
            companion=companion,
            discovery=companion_discovery,
            store=companion_store,
            status=companion_status,
            set_enabled=companion_set_enabled,
            set_show_intermediate_events=companion_store.set_show_intermediate_events,
        )

    def publish_codex(records: list) -> None:
        codex_state.record_snapshot()
        for record in records:
            hub.publish(record)
        # A completion/failure/queue change must reach a home screen without
        # waiting for the periodic UDP beat.
        home_counters.notify()

    def open_logs() -> None:
        os.startfile(data_dir)

    restart_requested = threading.Event()

    def restart_process() -> None:
        restart_requested.set()

    def device_registration_action(action: str, device_id: str) -> bool:
        def sync_home_counter_target() -> None:
            _sync_home_counter_target(home_counters, registry, device_id)

        if action == "approve":
            approved = manager.approve(device_id)
            sync_home_counter_target()
            home_counters.notify()
            return approved
        if action == "reject":
            rejected = manager.reject(device_id)
            home_counters.unregister(device_id)
            return rejected
        if action == "revoke":
            revoked = manager.revoke(device_id)
            home_counters.unregister(device_id)
            return revoked
        raise ValueError("registration_action_invalid")

    admin = AdminApi(
        hub,
        registry,
        on_device_removed=manager.remove,
        codex_status=codex_state.snapshot,
        open_logs=open_logs,
        companion_status=companion_status,
        companion_action=companion_action,
        device_registration_action=device_registration_action,
        codex_action=codex_action,
    )
    try:
        admin.start()
    except OSError as error:
        # The admin port is the front door for the settings page, so an
        # occupied port has to be explained rather than raised as a traceback.
        if port_is_in_use("127.0.0.1", ADMIN_PORT):
            emit(f"端口 {ADMIN_PORT} 已被另一个程序占用，CodexBridge 无法启动。请退出那个程序后重试。错误：{error}")
        else:
            emit(f"CodexBridge 无法监听端口 {ADMIN_PORT}：{error}")
        projection.close()
        event_spine.close()
        references.close()
        _release_instance_lock()
        return 1
    if companion_store.enabled:
        try:
            companion_service.start()
            companion.start()
            companion_discovery.start()
            print(f"CodexBridge companion API: {companion.url}", flush=True)
        except Exception as error:  # noqa: BLE001
            emit(f"CodexBridge companion API 启动失败：{error}", flush=True)
    stop = threading.Event()
    codex_stop = threading.Event()
    home_counters = HomeCounterSender(hub.counts)
    home_counters.start()
    codex_thread = threading.Thread(
        target=run_codex_collector,
        kwargs={
            "runtime_folder": data_dir,
            "cwd": codex_cwd,
            "interval": COLLECT_SECONDS,
            "stop": codex_stop,
            "publish": publish_codex,
            "codex_command": codex_command,
            "source_factory": lambda: CodexAppServerSource(
                data_dir,
                codex_cwd,
                codex_command,
                client=shared_app_server,
                owns_client=False,
            ),
            "report": codex_state.report,
            # The collector itself stays enabled: it is what fills the store
            # every device reads. Only its cadence is controlled below, so
            # slowing down never means a device sees an empty task list.
            "enabled": None,
            "on_source": source_holder.set,
            # Fast beat whenever somebody is connected OR the user explicitly
            # asked for always-on background collection. With the switch off
            # (the default) and nothing connected, drop to a slow beacon: the
            # data keeps flowing, just far less often. The first device or
            # phone to appear restores the original 5 s cadence immediately and
            # forces a refresh, so live behaviour is unchanged for any client
            # that is actually there.
            "interval_for": lambda: (
                COLLECT_SECONDS if (watcher_demand() or codex_state.snapshot()["enabled"])
                else IDLE_COLLECT_SECONDS
            ),
            "demand": watcher_demand,
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

    def on_presence(result) -> None:
        # A device that announces itself is already listening, so it does not
        # wait for the next broadcast round.
        print(f"CodexBridge: {result.device_id} announced itself at {result.host}")
        manager.sync([result])
        # A pending device still gets discovered and can be approved, but the
        # aggregate Codex counters must not leak over UDP before the user has
        # paired it.
        _sync_home_counter_target(
            home_counters, registry, result.device_id, result.host
        )

    presence = PresenceListener(on_presence, logger=print)
    presence.start()

    def poll_speech() -> None:
        while not stop.wait(0.25):
            # The Codex desktop is one of the clients in the unified device
            # protocol. When a completed task is opened there, import that read
            # so the phone and both ESPs retire the same badge and count.
            sync_desktop_read_state(read_state, store=messages)
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
        presence.stop()
        home_counters.stop()
        manager.stop()
        speech.close()
        companion_discovery.stop()
        companion.stop()
        cbdp_http.close_all()
        companion_service.stop()
        admin.stop()
        speech_thread.join(timeout=1.0)
        codex_thread.join(timeout=1.0)
        discovery_thread.join(timeout=1.0)
        # Close the fallback client only after the collector has stopped using
        # the source holder; closing it first can race an in-flight request.
        source_holder.close()
        shared_app_server.close()
        projection.close()
        event_spine.close()
        references.close()
    if restart_requested.is_set():
        # Hand the ports to the replacement instead of competing with it.
        _release_instance_lock()
        _launch_restarted_process(data_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
