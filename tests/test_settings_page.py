import re
import unittest

from codexbridge.settings_page import SETTINGS_HTML


class SettingsPageTest(unittest.TestCase):
    def test_page_defaults_to_simplified_chinese(self):
        self.assertIn('<html lang="zh-CN">', SETTINGS_HTML)
        self.assertIn("CodexBridge 管理中心", SETTINGS_HTML)

    def test_page_has_six_routes_in_the_required_order(self):
        routes = re.findall(r'data-route-link="([^"]+)"', SETTINGS_HTML)
        self.assertEqual(
            routes,
            ["overview", "devices", "queue", "access", "speech", "preferences"],
        )
        for route in routes:
            self.assertIn(f'href="#{route}"', SETTINGS_HTML)
            self.assertIn(f'id="{route}-panel"', SETTINGS_HTML)

    def test_pending_alert_is_an_actionable_button(self):
        self.assertRegex(SETTINGS_HTML, r'<button[^>]+id="pending-alert"[^>]*>')
        self.assertIn('data-action="show-pending"', SETTINGS_HTML)
        self.assertIn("台设备待批准", SETTINGS_HTML)

    def test_device_table_has_scan_friendly_columns(self):
        columns = re.findall(r'data-column="([^"]+)"', SETTINGS_HTML)
        self.assertEqual(
            columns,
            ["state", "device", "capabilities", "last-seen", "actions"],
        )
        for heading in ["状态", "设备", "能力", "最近连接", "操作"]:
            self.assertIn(f">{heading}<", SETTINGS_HTML)

    def test_pending_device_rows_keep_detail_shortcut(self):
        pending_branch = re.search(
            r'function rowActions\(model\) \{[\s\S]*?if \(model\.registrationState === "pending"\) \{[\s\S]*?\n    \}',
            SETTINGS_HTML,
        )
        self.assertIsNotNone(pending_branch)
        self.assertIn('actionButton("批准"', pending_branch.group(0))
        self.assertIn('actionButton("拒绝"', pending_branch.group(0))
        self.assertIn('actionButton("详情", "open-drawer"', pending_branch.group(0))

    def test_approved_hardware_can_be_deleted_for_rediscovery(self):
        hardware_branch = re.search(
            r'else if \(model\.registrationState === "approved" && model\.kind === "hardware"\) \{[\s\S]*?\n    \}',
            SETTINGS_HTML,
        )
        self.assertIsNotNone(hardware_branch)
        self.assertIn('actionButton("删除设备"', hardware_branch.group(0))
        self.assertIn('"remove-hardware"', hardware_branch.group(0))
        self.assertIn('/api/admin/devices/remove', SETTINGS_HTML)
        self.assertIn('data-action="drawer-remove-hardware"', SETTINGS_HTML)

    def test_device_filters_are_restored_from_session_storage(self):
        self.assertIn("codexbridge.deviceFilter", SETTINGS_HTML)
        self.assertIn("sessionStorage.getItem", SETTINGS_HTML)
        self.assertIn("sessionStorage.setItem", SETTINGS_HTML)
        self.assertIn("normalizeDeviceFilter", SETTINGS_HTML)

    def test_mobile_card_errors_are_visible_when_loaded(self):
        self.assertIn(".device-cards > .inline-alert", SETTINGS_HTML)
        self.assertIn('cards.appendChild(emptyFailure(error, "devices"))', SETTINGS_HTML)

    def test_device_drawer_has_complete_configuration_and_danger_actions(self):
        self.assertIn('id="device-drawer"', SETTINGS_HTML)
        self.assertIn('role="dialog"', SETTINGS_HTML)
        self.assertIn('id="drawer-capabilities"', SETTINGS_HTML)
        self.assertIn('id="drawer-budget"', SETTINGS_HTML)
        self.assertIn('data-action="drawer-save"', SETTINGS_HTML)
        self.assertIn('data-action="drawer-revoke"', SETTINGS_HTML)
        self.assertIn('data-action="drawer-remove-phone"', SETTINGS_HTML)

    def test_intermediate_events_live_only_in_preferences(self):
        preferences = re.search(
            r'<section[^>]+id="preferences-panel"[\s\S]*?</section>',
            SETTINGS_HTML,
        )
        access = re.search(
            r'<section[^>]+id="access-panel"[\s\S]*?</section>',
            SETTINGS_HTML,
        )
        self.assertIsNotNone(preferences)
        self.assertIsNotNone(access)
        self.assertIn("显示中间事件", preferences.group(0))
        self.assertNotIn("显示中间事件", access.group(0))

    def test_codex_collection_switch_lives_in_preferences(self):
        # The switch controls the idle cadence, so it belongs next to the other
        # run settings.
        preferences = SETTINGS_HTML.index('id="preferences-panel"')
        access = SETTINGS_HTML.index('id="access-panel"')
        switch = SETTINGS_HTML.index('id="codex-collector-enabled"')
        self.assertGreater(switch, preferences)
        self.assertGreater(switch, access)
        self.assertIn("Codex 采集", SETTINGS_HTML)
        self.assertIn("saveCodexCollector", SETTINGS_HTML)
        self.assertIn("applyCodexCollector", SETTINGS_HTML)

    def test_codex_collection_copy_promises_no_lost_functionality(self):
        # The copy must describe a cadence change, not a feature cut: clients
        # that connect always get full-speed collection.
        self.assertIn("空闲时降低采集频率", SETTINGS_HTML)
        self.assertIn("立刻恢复全速采集", SETTINGS_HTML)
        self.assertNotIn("任务列表会停止更新", SETTINGS_HTML)

    def test_queue_is_hidden_until_an_admin_queue_api_is_available(self):
        self.assertRegex(SETTINGS_HTML, r'id="queue-nav-item"[^>]*\bhidden\b')
        self.assertIn("/api/admin/queues", SETTINGS_HTML)
        self.assertIn("queueCanEdit", SETTINGS_HTML)

    def test_expected_management_apis_are_consumed(self):
        for path in [
            "/api/admin/status",
            "/api/admin/codex-status",
            "/api/admin/devices",
            "/api/admin/devices/approve",
            "/api/admin/devices/reject",
            "/api/admin/devices/revoke",
            "/api/admin/devices/capabilities",
            "/api/admin/companion",
            "/api/admin/settings/general",
            "/api/admin/settings/speech",
            "/api/admin/settings/speech/test",
        ]:
            self.assertIn(path, SETTINGS_HTML)

    def test_page_avoids_native_alert_and_confirm(self):
        self.assertNotIn("window.alert(", SETTINGS_HTML)
        self.assertNotIn("window.confirm(", SETTINGS_HTML)
        self.assertIn('id="confirm-dialog"', SETTINGS_HTML)
        self.assertIn('id="toast-region"', SETTINGS_HTML)

    def test_page_keeps_port_rules_and_access_fields(self):
        self.assertIn("手机服务端口", SETTINGS_HTML)
        self.assertIn("手机发现端口", SETTINGS_HTML)
        self.assertIn("8790 已保留", SETTINGS_HTML)
        self.assertIn('id="companion-port-list"', SETTINGS_HTML)
        self.assertIn('action: "set_port"', SETTINGS_HTML)

    def test_page_has_responsive_application_shell(self):
        self.assertIn("grid-template-columns: 232px minmax(0, 1fr)", SETTINGS_HTML)
        self.assertIn("@media (max-width: 760px)", SETTINGS_HTML)
        self.assertIn("@media (prefers-reduced-motion: reduce)", SETTINGS_HTML)
        self.assertIn("min-width: 320px", SETTINGS_HTML)
        self.assertIn("overflow-wrap: anywhere", SETTINGS_HTML)

    def test_page_does_not_depend_on_external_assets(self):
        self.assertNotIn("https://fonts.", SETTINGS_HTML)
        self.assertNotIn("cdn.", SETTINGS_HTML)

    def test_page_has_single_device_data_surface(self):
        self.assertEqual(SETTINGS_HTML.count('id="device-table-body"'), 1)
        self.assertEqual(SETTINGS_HTML.count('id="device-card-list"'), 1)
        self.assertEqual(SETTINGS_HTML.count('id="device-list"'), 0)


if __name__ == "__main__":
    unittest.main()
