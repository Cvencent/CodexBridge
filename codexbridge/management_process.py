"""Supervise the loopback Dotii management child process."""

from __future__ import annotations

import subprocess
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable, Deque


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MANAGEMENT_URL = "http://127.0.0.1:8787/"


def _display_path(executable: Path) -> str:
    """Return a stable path string without re-normalizing Windows separators."""
    return str(executable)


def management_argv(executable: Path, frozen: bool) -> list[str]:
    """Return the argv that starts only the hidden management child."""
    resolved = _display_path(executable)
    if frozen:
        return [resolved, "--management-child"]
    return [resolved, "-m", "codexbridge", "--management-child"]


def _esptool_marker_index(argv: list[str] | None) -> int:
    if not argv:
        return -1
    if argv[0] == "--esptool":
        return 0
    if len(argv) > 1 and argv[1] == "--esptool":
        return 1
    return -1


def is_esptool_argv(argv: list[str] | None) -> bool:
    """Return whether this process was launched only to proxy esptool."""
    return _esptool_marker_index(argv) >= 0


def esptool_arguments(argv: list[str] | None) -> list[str]:
    """Return arguments after the internal esptool marker."""
    index = _esptool_marker_index(argv)
    return [] if index < 0 else list(argv[index + 1:])


def run_esptool(argv: list[str] | None) -> int:
    """Run the bundled esptool without importing the tray or management host."""
    import esptool

    original = sys.argv
    try:
        sys.argv = ["esptool", *esptool_arguments(argv)]
        result = esptool.main()
        return 0 if result is None else int(result)
    finally:
        sys.argv = original


def run_management_child(argv: list[str] | None = None) -> int:
    """Run the loopback management child without importing the tray."""
    from codexbridge.management import main as management_main

    return int(management_main(list(argv or ["--no-browser"])))


class ManagementProcess:
    """Own, monitor, and restart the hidden management child process."""

    def __init__(
        self,
        executable: Path,
        frozen: bool,
        *,
        poll_interval: float = 1.0,
        restart_limit: int = 3,
        restart_window_seconds: float = 60.0,
        popen: Callable[..., subprocess.Popen] | None = None,
        platform_name: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.executable = Path(executable)
        self.frozen = frozen
        self.poll_interval = poll_interval
        self.restart_limit = restart_limit
        self.restart_window_seconds = restart_window_seconds
        self._popen = popen or subprocess.Popen
        self._platform_name = platform_name or sys.platform
        self._clock = clock
        self._process: subprocess.Popen | None = None
        self._restart_times: Deque[float] = deque()
        self.failed = False
        self.last_error = ""

    @property
    def url(self) -> str:
        return MANAGEMENT_URL

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self.running:
            return
        self.failed = False
        self.last_error = ""
        self._restart_times.clear()
        self._spawn()

    def _spawn(self) -> None:
        kwargs: dict[str, object] = {
            "close_fds": self._platform_name != "nt",
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if self._platform_name == "nt":
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        self._process = self._popen(
            management_argv(self.executable, self.frozen),
            **kwargs,
        )

    def poll(self) -> int | None:
        if self._process is None:
            return None
        code = self._process.poll()
        if code is None or code == 0:
            return code
        now = self._clock()
        self._restart_times.append(now)
        while (
            self._restart_times
            and now - self._restart_times[0] > self.restart_window_seconds
        ):
            self._restart_times.popleft()
        if len(self._restart_times) >= self.restart_limit:
            self.failed = True
            self.last_error = "管理中心子进程多次异常退出，已停止自动重启"
            return code
        self._spawn()
        return code

    def stop(self, timeout: float = 5.0) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
        except OSError:
            return
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                return
            process.wait()
