"""Pure-function tests for the management page logic.

The helpers live in JavaScript inside ``codexbridge.settings_ui.script`` so the
browser and the tests exercise the same code. Each case runs the real source
through Node instead of re-implementing the helpers in Python.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import textwrap
import unittest

from codexbridge.settings_ui.script import LOGIC_JS


NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "Node.js is required for management page logic tests")
class SettingsPageLogicTest(unittest.TestCase):
    def evaluate(self, expression: str):
        script = textwrap.dedent(
            """
            %s
            const logic = globalThis.__CB_LOGIC__;
            const value = (%s);
            process.stdout.write(JSON.stringify(value));
            """
            % (LOGIC_JS, expression)
        )
        completed = subprocess.run(
            [NODE, "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        self.assertEqual(
            completed.returncode,
            0,
            f"node failed: {completed.stderr}",
        )
        return json.loads(completed.stdout)

    def test_sort_devices_orders_pending_online_then_offline(self):
        devices = [
            {"device_id": "offline-old", "registration_state": "approved", "online": False, "last_seen": 100},
            {"device_id": "online-old", "registration_state": "approved", "online": True, "last_seen": 200},
            {"device_id": "pending-late", "registration_state": "pending", "last_seen": 900},
            {"device_id": "online-new", "registration_state": "approved", "online": True, "last_seen": 800},
            {"device_id": "pending-early", "registration_state": "pending", "last_seen": 300},
            {"device_id": "revoked", "registration_state": "revoked", "last_seen": 950},
        ]
        ordered = self.evaluate(
            "logic.sortDevices(%s).map((item) => item.device_id)" % json.dumps(devices)
        )
        self.assertEqual(
            ordered,
            ["pending-early", "pending-late", "online-new", "online-old", "offline-old", "revoked"],
        )

    def test_filter_devices_combines_status_kind_and_query(self):
        devices = [
            {"device_id": "hw-1", "kind": "hardware", "name": "车间网关", "registration_state": "pending"},
            {"device_id": "hw-2", "kind": "hardware", "name": "实验室网关", "registration_state": "approved"},
            {"device_id": "phone-1", "kind": "phone", "name": "我的手机", "registration_state": "approved"},
        ]
        payload = json.dumps(devices)
        self.assertEqual(
            len(self.evaluate('logic.filterDevices(%s, {status: "pending"})' % payload)),
            1,
        )
        self.assertEqual(
            len(self.evaluate('logic.filterDevices(%s, {kind: "hardware"})' % payload)),
            2,
        )
        self.assertEqual(
            len(
                self.evaluate(
                    'logic.filterDevices(%s, {status: "approved", kind: "hardware", query: "实验室"})'
                    % payload
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                self.evaluate(
                    'logic.filterDevices(%s, {status: "approved", kind: "hardware", query: "手机"})'
                    % payload
                )
            ),
            0,
        )

    def test_normalize_device_filter_rejects_unknown_values(self):
        result = self.evaluate(
            "logic.normalizeDeviceFilter({status:'unknown',kind:'tablet',query:123,sort:'bad'})"
        )
        self.assertEqual(
            result,
            {"status": "all", "kind": "all", "query": "", "sort": "default"},
        )
        retained = self.evaluate(
            "logic.normalizeDeviceFilter({status:'pending',kind:'hardware',query:'网关',sort:'name'})"
        )
        self.assertEqual(
            retained,
            {"status": "pending", "kind": "hardware", "query": "网关", "sort": "name"},
        )

    def test_device_credential_label_distinguishes_pending_devices(self):
        result = self.evaluate(
            "["
            "logic.deviceCredentialLabel({registration_state:'pending',has_token:false}),"
            "logic.deviceCredentialLabel({registration_state:'approved',has_token:false}),"
            "logic.deviceCredentialLabel({registration_state:'approved',has_token:true})"
            "]"
        )
        self.assertEqual(result, ["等待批准", "缺少令牌", "已登记"])

    def test_format_relative_time_buckets(self):
        now = 1_800_000_000
        self.assertEqual(self.evaluate("logic.formatRelativeTime(%d, %d)" % (now, now - 5)), "刚刚")
        self.assertEqual(
            self.evaluate("logic.formatRelativeTime(%d, %d)" % (now, now - 600)),
            "10 分钟前",
        )
        self.assertEqual(
            self.evaluate("logic.formatRelativeTime(%d, %d)" % (now, now - 7200)),
            "2 小时前",
        )
        self.assertEqual(self.evaluate("logic.formatRelativeTime(%d, 0)" % now), "从未")

    def test_map_device_accepts_legacy_and_unified_fields(self):
        legacy = {
            "phone_id": "phone-legacy",
            "device_name": "旧版手机",
            "capabilities": ["status", "voice", "voice"],
            "enabled_capabilities": ["status"],
            "last_seen_at": 1234,
        }
        unified = {
            "device_id": "hw-9",
            "kind": "hardware",
            "name": "统一硬件",
            "registration_state": "revoked",
            "supported": ["text"],
            "enabled": ["text"],
            "max_text_chars": 400,
            "has_token": True,
            "queued_count": 3,
        }
        mapped = self.evaluate(
            "[(function(){const m=logic.mapDeviceToViewModel(%s);return {id:m.deviceId,kind:m.kind,"
            "name:m.name,supported:m.supported,enabled:m.enabled,lastSeen:m.lastSeen,online:m.online};})(),"
            "(function(){const m=logic.mapDeviceToViewModel(%s);return {id:m.deviceId,kind:m.kind,"
            "state:m.registrationState,budget:m.maxTextChars,token:m.hasToken,queued:m.queuedCount};})()]"
            % (json.dumps(legacy), json.dumps(unified))
        )
        legacy_model, unified_model = mapped
        self.assertEqual(legacy_model["id"], "phone-legacy")
        self.assertEqual(legacy_model["kind"], "phone")
        self.assertEqual(legacy_model["name"], "旧版手机")
        self.assertEqual(legacy_model["supported"], ["status", "voice"])
        self.assertEqual(legacy_model["enabled"], ["status"])
        self.assertEqual(legacy_model["lastSeen"], 1234)
        self.assertFalse(legacy_model["online"])
        self.assertEqual(unified_model["id"], "hw-9")
        self.assertEqual(unified_model["kind"], "hardware")
        self.assertEqual(unified_model["state"], "revoked")
        self.assertEqual(unified_model["budget"], 400)
        self.assertTrue(unified_model["token"])
        self.assertEqual(unified_model["queued"], 3)

    def test_map_device_is_idempotent_for_view_models(self):
        source = {"device_id": "hw-1", "kind": "hardware", "registration_state": "approved", "online": True, "last_seen": 50}
        result = self.evaluate(
            "(function(){const once=logic.mapDeviceToViewModel(%s);"
            "const twice=logic.mapDeviceToViewModel(once);"
            "return {id:twice.deviceId,state:twice.registrationState,online:twice.online,lastSeen:twice.lastSeen};})()"
            % json.dumps(source)
        )
        self.assertEqual(result["id"], "hw-1")
        self.assertEqual(result["state"], "approved")
        self.assertTrue(result["online"])
        self.assertEqual(result["lastSeen"], 50)

    def test_queue_action_predicates(self):
        result = self.evaluate(
            "["
            "[logic.queueCanEdit({status:'pending'}),logic.queueCanSteer({status:'pending'}),logic.queueCanDelete({status:'pending'})],"
            "[logic.queueCanEdit({status:'failed'}),logic.queueCanSteer({status:'failed'}),logic.queueCanDelete({status:'failed'})],"
            "[logic.queueCanEdit({status:'dispatching'}),logic.queueCanSteer({status:'dispatching'}),logic.queueCanDelete({status:'dispatching'})]"
            "]"
        )
        self.assertEqual(result[0], [True, True, True])
        self.assertEqual(result[1], [True, False, True])
        self.assertEqual(result[2], [False, False, False])


if __name__ == "__main__":
    unittest.main()
