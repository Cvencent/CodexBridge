from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from codexbridge import management_process


class _FakeProcess:
    def __init__(self, *, returncode: int | None = None) -> None:
        self.returncode = returncode
        self.terminate_called = False
        self.kill_called = False
        self.wait_calls: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminate_called = True

    def kill(self) -> None:
        self.kill_called = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int | None:
        self.wait_calls.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired("CodexBridge", timeout)
        return self.returncode


class _FakePopenFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict]] = []
        self.processes: list[_FakeProcess] = []

    def __call__(self, argv: list[str], **kwargs):
        process = _FakeProcess(returncode=None)
        self.calls.append((argv, kwargs))
        self.processes.append(process)
        return process


class ManagementProcessTest(unittest.TestCase):
    def test_esptool_argv_is_forwarded_without_starting_the_tray(self) -> None:
        argv = ["C:/Apps/CodexBridge.exe", "--esptool", "--chip", "esp32s3", "write-flash"]
        self.assertTrue(management_process.is_esptool_argv(argv))
        self.assertEqual(
            management_process.esptool_arguments(argv),
            ["--chip", "esp32s3", "write-flash"],
        )

    def test_normal_argv_is_not_an_esptool_invocation(self) -> None:
        self.assertFalse(management_process.is_esptool_argv(["C:/Apps/CodexBridge.exe", "--no-tray"]))
        self.assertEqual(
            management_process.esptool_arguments(["C:/Apps/CodexBridge.exe", "--no-tray"]),
            [],
        )

    def test_esptool_none_result_is_a_success_exit_code(self) -> None:
        with mock.patch("esptool.main", return_value=None):
            self.assertEqual(management_process.run_esptool(["--esptool", "version"]), 0)

    def test_main_dispatches_esptool_before_argparse(self) -> None:
        from codexbridge import __main__ as bridge_main

        with mock.patch.object(bridge_main, "run_esptool", return_value=7) as runner:
            code = bridge_main.main(["--esptool", "--version"])

        self.assertEqual(code, 7)
        self.assertEqual(runner.call_args.args[0], ["--esptool", "--version"])

    def test_frozen_host_launches_itself_as_hidden_management_child(self) -> None:
        executable = Path("C:/Apps/CodexBridge.exe")
        self.assertEqual(
            management_process.management_argv(executable, True),
            [str(executable), "--management-child"],
        )

    def test_source_host_launches_the_module_as_a_management_child(self) -> None:
        argv = management_process.management_argv(Path("C:/Python/python.exe"), False)
        self.assertEqual(argv[1:], ["-m", "codexbridge", "--management-child"])

    def test_start_uses_hidden_window_flags(self) -> None:
        factory = _FakePopenFactory()
        process = management_process.ManagementProcess(
            Path("C:/Apps/CodexBridge.exe"),
            True,
            popen=factory,
            platform_name="nt",
        )
        process.start()
        self.assertEqual(len(factory.calls), 1)
        argv, kwargs = factory.calls[0]
        self.assertEqual(argv, [str(Path("C:/Apps/CodexBridge.exe")), "--management-child"])
        self.assertTrue(kwargs["creationflags"] & management_process.CREATE_NO_WINDOW)
        self.assertFalse(kwargs["close_fds"])

    def test_stop_terminates_then_kills_after_timeout(self) -> None:
        factory = _FakePopenFactory()
        process = management_process.ManagementProcess(
            Path("C:/Apps/CodexBridge.exe"),
            True,
            popen=factory,
            platform_name="nt",
        )
        process.start()
        child = factory.processes[0]

        process.stop(timeout=0.01)
        self.assertTrue(child.terminate_called)
        self.assertTrue(child.kill_called)

    def test_three_unexpected_exits_stop_restarting(self) -> None:
        factory = _FakePopenFactory()
        process = management_process.ManagementProcess(
            Path("C:/Apps/CodexBridge.exe"),
            True,
            popen=factory,
            platform_name="nt",
        )
        process.start()

        for index in range(3):
            factory.processes[index].returncode = 1
            process.poll()

        self.assertEqual(len(factory.processes), 3)
        self.assertEqual(process.poll(), 1)
        self.assertEqual(len(factory.processes), 3)
        self.assertTrue(process.failed)

    def test_management_child_runs_the_management_main_without_tray(self) -> None:
        with (
            mock.patch("codexbridge.management.main", return_value=0) as management_main,
            mock.patch("codexbridge.__main__.tray", create=True) as tray_module,
        ):
            code = management_process.run_management_child(["--no-browser"])
        self.assertEqual(code, 0)
        management_main.assert_called_once_with(["--no-browser"])
        self.assertFalse(tray_module.called)


if __name__ == "__main__":
    unittest.main()


