"""Tests for companion port validation and status reporting."""

from __future__ import annotations

import unittest

from codexbridge.companion.store import DEFAULT_PORT, LEGACY_PORT
from codexbridge.ports import build_port_statuses, validate_companion_port


class PortSettingsTests(unittest.TestCase):
    def test_validation_rejects_the_reserved_legacy_port(self) -> None:
        with self.assertRaisesRegex(ValueError, "port_reserved"):
            validate_companion_port(LEGACY_PORT)

    def test_validation_accepts_the_default_port(self) -> None:
        self.assertEqual(validate_companion_port(DEFAULT_PORT), DEFAULT_PORT)

    def test_statuses_expose_health_and_fixed_ports(self) -> None:
        ports = build_port_statuses(
            admin_port=8788,
            companion_port=DEFAULT_PORT,
            companion_enabled=True,
            companion_running=True,
            discovery_running=True,
            admin_running=True,
        )
        by_key = {item["key"]: item for item in ports}
        self.assertEqual(by_key["admin"]["state"], "running")
        self.assertEqual(by_key["companion"]["state"], "running")
        self.assertTrue(by_key["companion"]["editable"])
        self.assertEqual(by_key["discovery"]["state"], "running")
        self.assertEqual(by_key["reserved"]["port"], LEGACY_PORT)
        self.assertEqual(by_key["reserved"]["state"], "reserved")

    def test_statuses_report_companion_problem_when_enabled_but_not_bound(self) -> None:
        ports = build_port_statuses(
            admin_port=8788,
            companion_port=DEFAULT_PORT,
            companion_enabled=True,
            companion_running=False,
            discovery_running=False,
            admin_running=True,
        )
        by_key = {item["key"]: item for item in ports}
        self.assertEqual(by_key["companion"]["state"], "error")
        self.assertEqual(by_key["discovery"]["state"], "error")


if __name__ == "__main__":
    unittest.main()
