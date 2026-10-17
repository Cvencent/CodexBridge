from __future__ import annotations


BASE_CSS = r"""
:root {
  color-scheme: light;
  --bg: #f3f5f7;
  --surface: #ffffff;
  --surface-subtle: #f8fafb;
  --border: #d8dee4;
  --border-strong: #b8c2cc;
  --text: #17212b;
  --text-muted: #5f6b76;
  --accent: #176b87;
  --accent-hover: #12566d;
  --accent-soft: #e7f3f7;
  --success: #217a4b;
  --success-soft: #e8f5ed;
  --warning: #8a5a00;
  --warning-soft: #fff4d6;
  --danger: #b42318;
  --danger-soft: #fdecea;
  --neutral-soft: #eef1f4;
  --radius: 8px;
  --shadow-1: 0 1px 2px rgba(16, 24, 40, 0.06);
  --z-topbar: 100;
  --z-drawer: 300;
  --z-confirm: 400;
  --z-toast: 500;
}
* { box-sizing: border-box; }
html {
  min-width: 320px;
  background: var(--bg);
  scroll-behavior: smooth;
}
body {
  margin: 0;
  min-height: 100dvh;
  color: var(--text);
  background: var(--bg);
  font: 15px/1.55 "Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif;
  font-variant-numeric: tabular-nums;
}
button, input, select, textarea { font: inherit; }
button, input, select, textarea { touch-action: manipulation; }
button { letter-spacing: 0; }
[hidden] { display: none !important; }
.skip-link {
  position: fixed;
  top: 8px;
  left: 8px;
  z-index: var(--z-toast);
  padding: 8px 12px;
  color: #fff;
  background: var(--accent);
  border-radius: 6px;
  transform: translateY(-150%);
}
.skip-link:focus { transform: translateY(0); }
.svg-sprite { position: absolute; width: 0; height: 0; overflow: hidden; }
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  padding: 0;
  overflow: hidden;
  clip: rect(0, 0, 0, 0);
  white-space: nowrap;
  border: 0;
}
.switch {
  flex: 0 0 auto;
  width: 46px;
  height: 26px;
  min-height: 26px;
  margin: 0;
  padding: 0;
  appearance: none;
  background: var(--border-strong);
  border: 1px solid var(--border-strong);
  border-radius: 999px;
  cursor: pointer;
  position: relative;
  transition: background 120ms ease, border-color 120ms ease;
}
.switch::after {
  content: "";
  position: absolute;
  top: 2px;
  left: 2px;
  width: 20px;
  height: 20px;
  background: var(--surface);
  border-radius: 999px;
  box-shadow: var(--shadow-1);
  transition: transform 120ms ease;
}
.switch:checked { background: var(--accent); border-color: var(--accent); }
.switch:checked::after { transform: translateX(20px); }
.switch:disabled { cursor: not-allowed; opacity: 0.5; }
body.drawer-open { overflow: hidden; }
.icon { width: 18px; height: 18px; flex: 0 0 auto; stroke-width: 1.8; }
.icon-sm { width: 16px; height: 16px; }
.icon-lg { width: 20px; height: 20px; }

.topbar {
  position: sticky;
  top: 0;
  z-index: var(--z-topbar);
  border-bottom: 1px solid var(--border);
  background: rgba(255, 255, 255, 0.96);
  backdrop-filter: blur(10px);
}
.topbar-inner {
  width: min(1440px, calc(100% - 32px));
  min-height: 64px;
  margin: 0 auto;
  display: flex;
  align-items: center;
  gap: 16px;
}
.brand { display: flex; align-items: center; gap: 10px; min-width: 190px; }
.brand-mark {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  color: #fff;
  background: var(--accent);
  border-radius: 8px;
}
.brand-copy strong { display: block; font-size: 17px; line-height: 1.15; }
.brand-copy span { color: var(--text-muted); font-size: 12.5px; }
.topbar-status { display: flex; align-items: center; gap: 10px; margin-left: auto; min-width: 0; }
.service-status {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 34px;
  color: var(--success);
  font-size: 13px;
  font-weight: 650;
  white-space: nowrap;
}
.service-status::before {
  content: "";
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: currentColor;
  box-shadow: 0 0 0 3px var(--success-soft);
}
.service-status[data-state="offline"] { color: var(--danger); }
.service-status[data-state="offline"]::before { box-shadow: 0 0 0 3px var(--danger-soft); }
.pending-alert {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  min-height: 34px;
  padding: 6px 11px;
  color: var(--warning);
  background: var(--warning-soft);
  border: 1px solid #edd28b;
  border-radius: 999px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 700;
}
.pending-alert:hover { border-color: var(--warning); }

.app-shell {
  width: min(1440px, calc(100% - 32px));
  margin: 18px auto 56px;
  display: grid;
  grid-template-columns: 232px minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}
.side-nav {
  position: sticky;
  top: 82px;
  display: grid;
  gap: 4px;
  padding: 8px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-1);
}
.route-link {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 42px;
  padding: 8px 10px;
  color: var(--text-muted);
  background: transparent;
  border: 0;
  border-radius: 6px;
  cursor: pointer;
  text-align: left;
  font-weight: 650;
}
.route-link:hover { color: var(--text); background: var(--surface-subtle); }
.route-link[aria-current="page"] { color: var(--accent); background: var(--accent-soft); }
.route-link .nav-count {
  margin-left: auto;
  min-width: 22px;
  padding: 1px 6px;
  color: var(--warning);
  background: var(--warning-soft);
  border-radius: 999px;
  text-align: center;
  font-size: 11px;
}
.main-content { min-width: 0; }
.page-panel { min-width: 0; }
.panel-heading {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.panel-heading h1,
.panel-heading h2 { margin: 0; font-size: 21px; line-height: 1.3; font-weight: 650; }
.panel-heading p { margin: 5px 0 0; color: var(--text-muted); font-size: 13px; }
.panel-heading-actions { display: flex; flex-wrap: wrap; gap: 8px; }
.surface {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow-1);
}
.surface + .surface { margin-top: 16px; }
.surface-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  padding: 16px 18px 12px;
  border-bottom: 1px solid var(--border);
}
.surface-header h2,
.surface-header h3 { margin: 0; font-size: 16px; line-height: 1.35; }
.surface-header p { margin: 4px 0 0; color: var(--text-muted); font-size: 12.5px; }
.surface-body { padding: 16px 18px 18px; }
.surface-body.tight { padding-top: 10px; }
.section-group + .section-group { margin-top: 22px; padding-top: 18px; border-top: 1px solid var(--border); }
.section-label { margin: 0 0 10px; color: var(--text-muted); font-size: 12px; font-weight: 700; }

.metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}
.metric {
  min-width: 0;
  padding: 13px 14px;
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}
.metric-label { color: var(--text-muted); font-size: 12px; }
.metric-value { display: block; margin-top: 3px; font-size: 23px; line-height: 1.2; font-weight: 700; }
.metric-detail { margin: 3px 0 0; color: var(--text-muted); font-size: 12px; }
.overview-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(260px, .65fr); gap: 16px; align-items: start; }
.summary-list { display: grid; gap: 8px; }
.summary-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 46px;
  padding: 9px 12px;
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: 6px;
}
.summary-row strong { display: block; font-size: 14px; }
.summary-row span { color: var(--text-muted); font-size: 12px; }
.quick-actions { display: flex; flex-wrap: wrap; gap: 8px; }

.toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 12px;
  border-bottom: 1px solid var(--border);
}
.search-field { position: relative; flex: 1 1 230px; min-width: 190px; }
.search-field .icon {
  position: absolute;
  left: 10px;
  top: 50%;
  color: var(--text-muted);
  transform: translateY(-50%);
  pointer-events: none;
}
.search-field input { width: 100%; padding-left: 34px; }
.field, .select-field { display: grid; gap: 6px; min-width: 0; }
.field label, .select-field label { font-size: 13px; font-weight: 650; }
.field-hint { margin: 0; color: var(--text-muted); font-size: 12.5px; }
.field-error { min-height: 18px; margin: 0; color: var(--danger); font-size: 12.5px; }
input, select, textarea {
  min-height: 36px;
  padding: 7px 9px;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  outline: none;
}
textarea { min-height: 96px; resize: vertical; }
input:hover, select:hover, textarea:hover { border-color: #94a1ad; }
input:focus-visible, select:focus-visible, textarea:focus-visible,
button:focus-visible, .route-link:focus-visible, .source-option:focus-within,
.device-row:focus-visible {
  outline: 3px solid rgba(23, 107, 135, 0.24);
  outline-offset: 2px;
  border-color: var(--accent);
}
input[aria-invalid="true"], textarea[aria-invalid="true"] { border-color: var(--danger); }
.button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  min-height: 36px;
  padding: 7px 12px;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border-strong);
  border-radius: 6px;
  cursor: pointer;
  font-weight: 650;
  white-space: nowrap;
}
.button:hover { background: #f3f6f8; border-color: #94a1ad; }
.button:active { transform: translateY(1px); }
.button:disabled { cursor: not-allowed; opacity: 0.5; transform: none; }
.button-primary { color: #fff; background: var(--accent); border-color: var(--accent); }
.button-primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
.button-danger { color: var(--danger); background: var(--surface); border-color: #e4a9a2; }
.button-danger:hover { background: var(--danger-soft); }
.button-ghost { color: var(--accent); background: transparent; border-color: transparent; }
.button-ghost:hover { background: var(--accent-soft); border-color: transparent; }
.button-icon { width: 36px; min-width: 36px; padding: 7px; }
.button-row, .form-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.form-actions { margin-top: 18px; padding-top: 16px; border-top: 1px solid var(--border); }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
.form-grid .span-2 { grid-column: 1 / -1; }
.key-row { display: flex; gap: 8px; align-items: stretch; }
.key-row input { flex: 1 1 auto; min-width: 0; }
.check-row {
  display: flex;
  align-items: center;
  gap: 9px;
  min-height: 40px;
  padding: 7px 9px;
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: 6px;
  cursor: pointer;
}
.check-row input { width: 18px; height: 18px; min-height: auto; margin: 0; accent-color: var(--accent); }

.device-table-wrap { overflow-x: auto; }
.device-table { width: 100%; border-collapse: collapse; }
.device-table th,
.device-table td { padding: 10px 12px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }
.device-table th { color: var(--text-muted); background: var(--surface-subtle); font-size: 12px; font-weight: 700; white-space: nowrap; }
.device-table tbody tr:last-child td { border-bottom: 0; }
.device-table tbody tr:hover { background: #fbfcfd; }
.device-table .col-state { width: 136px; }
.device-table .col-capabilities { width: 120px; }
.device-table .col-last-seen { width: 116px; }
.device-table .col-actions { width: 190px; }
.device-identity { display: grid; gap: 2px; min-width: 0; }
.device-identity strong { min-width: 0; font-size: 14px; overflow-wrap: anywhere; }
.device-identity span { color: var(--text-muted); font-size: 12px; overflow-wrap: anywhere; }
.device-identity code {
  color: var(--text-muted);
  font: 12px Consolas, "Courier New", monospace;
  overflow-wrap: anywhere;
}
.state-stack { display: grid; gap: 3px; }
.status-label {
  display: inline-flex;
  align-items: center;
  width: max-content;
  max-width: 100%;
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 700;
}
.status-label.approved { color: var(--success); background: var(--success-soft); }
.status-label.pending { color: var(--warning); background: var(--warning-soft); }
.status-label.rejected, .status-label.revoked { color: var(--danger); background: var(--danger-soft); }
.online-line { display: inline-flex; align-items: center; gap: 5px; color: var(--text-muted); font-size: 12px; }
.online-line::before { content: ""; width: 7px; height: 7px; border-radius: 999px; background: currentColor; }
.online-line.online { color: var(--success); }
.capability-count { display: inline-flex; align-items: baseline; gap: 4px; font-weight: 700; }
.capability-count small { color: var(--text-muted); font-weight: 400; }
.queue-badge {
  display: inline-flex;
  align-items: center;
  min-height: 22px;
  margin-left: 5px;
  padding: 1px 6px;
  color: var(--accent);
  background: var(--accent-soft);
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
}
.row-actions { display: flex; flex-wrap: wrap; gap: 6px; }
.row-actions .button { min-height: 32px; padding: 5px 8px; font-size: 12.5px; }
.device-cards { display: none; padding: 10px; gap: 10px; }
.device-cards > .inline-alert { grid-column: 1 / -1; }
.device-card {
  padding: 12px;
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: 6px;
}
.device-card-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 10px; }
.device-card-title { min-width: 0; }
.device-card-title strong { display: block; overflow-wrap: anywhere; }
.device-card-title span { display: block; margin-top: 2px; color: var(--text-muted); font-size: 12px; overflow-wrap: anywhere; }
.device-card-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 9px; color: var(--text-muted); font-size: 12px; }
.device-card-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
.empty-state, .empty-box {
  padding: 26px 16px;
  color: var(--text-muted);
  background: var(--surface-subtle);
  border: 1px dashed var(--border-strong);
  border-radius: var(--radius);
  text-align: center;
}
.empty-state strong, .empty-box strong { display: block; color: var(--text); }
.empty-state p, .empty-box p { margin: 5px 0 0; }
.inline-alert {
  margin: 0 0 12px;
  padding: 10px 12px;
  color: var(--text-muted);
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: 6px;
  font-size: 13px;
  overflow-wrap: anywhere;
}
.inline-alert[data-state="error"] { color: var(--danger); background: var(--danger-soft); border-color: #f3c0ba; }
.inline-alert[data-state="success"] { color: var(--success); background: var(--success-soft); border-color: #b8dfc7; }
.inline-alert[data-state="working"] { color: var(--warning); background: var(--warning-soft); border-color: #edd28b; }
.muted { color: var(--text-muted); overflow-wrap: anywhere; }
.mono { font-family: Consolas, "Courier New", monospace; overflow-wrap: anywhere; }
.tooltip-time { border-bottom: 1px dotted var(--border-strong); cursor: help; }

.drawer-backdrop {
  position: fixed;
  inset: 0;
  z-index: var(--z-drawer);
  background: rgba(15, 23, 42, 0.34);
}
.device-drawer {
  position: fixed;
  top: 0;
  right: 0;
  z-index: var(--z-drawer);
  width: min(440px, 42vw);
  min-width: 360px;
  height: 100dvh;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr) auto;
  background: var(--surface);
  border: 0;
  border-left: 1px solid var(--border);
  box-shadow: -8px 0 28px rgba(16, 24, 40, 0.12);
}
.drawer-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px;
  border-bottom: 1px solid var(--border);
}
.drawer-header h2 { margin: 0; font-size: 18px; overflow-wrap: anywhere; }
.drawer-header p { margin: 4px 0 0; color: var(--text-muted); font-size: 12.5px; }
.drawer-body { min-height: 0; padding: 16px 18px 24px; overflow-y: auto; }
.drawer-section + .drawer-section { margin-top: 20px; padding-top: 18px; border-top: 1px solid var(--border); }
.drawer-section h3 { margin: 0 0 10px; font-size: 14px; }
.drawer-facts { display: grid; grid-template-columns: 120px minmax(0, 1fr); gap: 7px 12px; margin: 0; }
.drawer-facts dt { color: var(--text-muted); font-size: 12.5px; }
.drawer-facts dd { margin: 0; min-width: 0; overflow-wrap: anywhere; }
.capability-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; }
.drawer-footer { padding: 12px 18px; background: var(--surface-subtle); border-top: 1px solid var(--border); }

.confirm-dialog, .queue-edit-dialog {
  width: min(440px, calc(100% - 28px));
  padding: 0;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  box-shadow: 0 20px 60px rgba(16, 24, 40, 0.2);
}
.confirm-dialog::backdrop, .queue-edit-dialog::backdrop { background: rgba(15, 23, 42, 0.38); }
.dialog-body { padding: 18px; }
.dialog-body h2 { margin: 0; font-size: 17px; }
.dialog-body p { margin: 8px 0 0; color: var(--text-muted); }
.dialog-actions { display: flex; justify-content: flex-end; gap: 8px; padding: 12px 18px; background: var(--surface-subtle); border-top: 1px solid var(--border); }

.toast-region {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: var(--z-toast);
  display: grid;
  gap: 8px;
  width: min(360px, calc(100% - 36px));
}
.toast {
  padding: 10px 12px;
  color: var(--text);
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 4px solid var(--accent);
  border-radius: 6px;
  box-shadow: 0 8px 24px rgba(16, 24, 40, 0.16);
  font-size: 13px;
}
.toast[data-state="success"] { border-left-color: var(--success); }
.toast[data-state="error"] { border-left-color: var(--danger); }
.toast[data-state="warning"] { border-left-color: var(--warning); }

.source-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin: 0 0 18px; padding: 0; border: 0; }
.source-options legend { grid-column: 1 / -1; padding: 0; }
.source-option {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-height: 62px;
  padding: 12px 13px;
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
}
.source-option:has(input:checked) { border-color: var(--accent); background: var(--accent-soft); box-shadow: inset 3px 0 0 var(--accent); }
.source-option input { width: 18px; height: 18px; min-height: auto; margin-top: 3px; accent-color: var(--accent); }
.source-option strong { display: block; }
.source-option span { display: block; margin-top: 2px; color: var(--text-muted); font-size: 12.5px; }
.pair-code {
  display: grid;
  gap: 5px;
  margin-top: 12px;
  padding: 14px;
  color: #f8fafc;
  background: #101828;
  border-radius: 8px;
  text-align: center;
  font: 700 30px/1.2 Consolas, "Courier New", monospace;
  letter-spacing: 6px;
}
.pair-code small { color: #cbd5e1; font: 400 12px/1.4 "Segoe UI", "Microsoft YaHei UI", sans-serif; letter-spacing: 0; }
.port-list { display: grid; gap: 8px; }
.port-row {
  display: grid;
  grid-template-columns: minmax(160px, 1fr) minmax(110px, 140px) minmax(150px, auto);
  align-items: center;
  gap: 12px;
  padding: 10px 12px;
  background: var(--surface-subtle);
  border: 1px solid var(--border);
  border-radius: 6px;
}
.port-row strong { display: block; font-size: 14px; }
.port-row p { margin: 2px 0 0; color: var(--text-muted); font-size: 12px; }
.port-number { font-family: Consolas, "Courier New", monospace; font-weight: 700; }
.port-actions-cell { display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-width: 0; }
.port-state { padding: 3px 8px; border-radius: 999px; color: var(--success); background: var(--success-soft); font-size: 12px; font-weight: 700; white-space: nowrap; }
.port-state[data-state="error"] { color: var(--danger); background: var(--danger-soft); }
.port-state[data-state="disabled"] { color: var(--text-muted); background: var(--neutral-soft); }
.port-state[data-state="reserved"] { color: var(--warning); background: var(--warning-soft); }
.queue-thread { padding: 14px; border: 1px solid var(--border); border-radius: 8px; background: var(--surface-subtle); }
.queue-thread + .queue-thread { margin-top: 10px; }
.queue-thread-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.queue-thread-head strong { display: block; overflow-wrap: anywhere; }
.queue-thread-head span { color: var(--text-muted); font-size: 12px; }
.queue-item { margin-top: 10px; padding-top: 10px; border-top: 1px solid var(--border); }
.queue-item-summary { margin: 0; white-space: pre-wrap; overflow-wrap: anywhere; }
.queue-item-meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 4px; color: var(--text-muted); font-size: 12px; }
.queue-item-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.skeleton { min-height: 44px; border-radius: 6px; background: linear-gradient(90deg, var(--surface-subtle), #edf1f4, var(--surface-subtle)); background-size: 200% 100%; animation: skeleton 1.4s linear infinite; }
@keyframes skeleton { to { background-position: -200% 0; } }

@media (max-width: 1180px) {
  .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .overview-grid { grid-template-columns: 1fr; }
  .device-table .col-capabilities, .device-table .col-last-seen { display: none; }
}
@media (max-width: 760px) {
  body { font-size: 15px; }
  .topbar-inner, .app-shell { width: min(100% - 24px, 1440px); }
  .topbar-inner { min-height: 58px; gap: 8px; }
  .brand { min-width: 0; }
  .brand-copy span { display: none; }
  .service-status { max-width: 180px; overflow: hidden; text-overflow: ellipsis; }
  .topbar-status { gap: 6px; }
  .pending-alert { padding: 5px 8px; }
  .app-shell { display: block; margin-top: 12px; }
  .side-nav {
    position: sticky;
    top: 58px;
    z-index: 90;
    display: flex;
    gap: 6px;
    margin: -12px -12px 14px;
    padding: 8px 12px;
    overflow-x: auto;
    border-right: 0;
    border-left: 0;
    border-radius: 0;
    box-shadow: none;
  }
  .route-link { flex: 0 0 auto; min-height: 38px; padding: 6px 10px; }
  .route-link .nav-count { margin-left: 2px; }
  .panel-heading { display: block; }
  .panel-heading-actions { margin-top: 10px; }
  .metrics, .form-grid, .source-options { grid-template-columns: 1fr; }
  .form-grid .span-2 { grid-column: auto; }
  .toolbar > * { flex: 1 1 100%; }
  .device-table-wrap { display: none; }
  .device-cards { display: grid; }
  .port-row { grid-template-columns: 1fr; align-items: stretch; }
  .port-actions-cell { justify-content: space-between; }
  .device-drawer { top: 8dvh; width: 100%; min-width: 0; height: 92dvh; border-left: 0; border-top: 1px solid var(--border); border-radius: 12px 12px 0 0; }
  .drawer-footer { position: sticky; bottom: 0; }
  .button { min-height: 44px; }
  .button-icon { width: 44px; min-width: 44px; }
  .toast-region { right: 12px; bottom: 12px; width: calc(100% - 24px); }
}
@media (max-width: 380px) {
  .topbar-status .service-status { display: none; }
  .metrics { gap: 8px; }
  .surface-header, .surface-body { padding-left: 12px; padding-right: 12px; }
  .drawer-facts { grid-template-columns: 1fr; gap: 2px; }
  .drawer-facts dd { margin-bottom: 8px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
  }
  .device-drawer, .toast { transform: none !important; }
}
"""
