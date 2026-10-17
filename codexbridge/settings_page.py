from __future__ import annotations

SETTINGS_HTML = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CodexBridge 管理中心</title>
  <link rel="icon" href="data:,">
  <style>
    :root {
      color-scheme: light;
      --bg: #f3f5f7;
      --surface: #ffffff;
      --surface-muted: #f8fafb;
      --border: #d8dee4;
      --border-strong: #b8c2cc;
      --text: #17212b;
      --muted: #5f6b76;
      --accent: #176b87;
      --accent-hover: #12566d;
      --accent-soft: #e7f3f7;
      --success: #217a4b;
      --success-soft: #e8f5ed;
      --danger: #b42318;
      --danger-soft: #fdecea;
      --warning: #8a5a00;
      --warning-soft: #fff4d6;
      --radius: 8px;
      --shadow: 0 1px 2px rgba(16, 24, 40, 0.06);
    }
    * { box-sizing: border-box; }
    html { min-width: 320px; background: var(--bg); }
    body {
      margin: 0;
      min-height: 100dvh;
      color: var(--text);
      background: var(--bg);
      font: 16px/1.55 "Segoe UI", "Microsoft YaHei UI", sans-serif;
    }
    button, input, select { font: inherit; }
    button, input, select { touch-action: manipulation; }
    .skip-link {
      position: fixed;
      top: 8px;
      left: 8px;
      z-index: 100;
      padding: 8px 12px;
      color: #fff;
      background: var(--accent);
      border-radius: 6px;
      transform: translateY(-150%);
    }
    .skip-link:focus { transform: translateY(0); }
    .topbar {
      border-bottom: 1px solid var(--border);
      background: var(--surface);
    }
    .topbar-inner {
      width: min(1120px, calc(100% - 40px));
      min-height: 68px;
      margin: 0 auto;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
    }
    .brand { display: flex; align-items: baseline; gap: 10px; }
    .brand strong { font-size: 20px; letter-spacing: 0; }
    .brand span { color: var(--muted); font-size: 14px; }
    .status-pill {
      display: inline-flex;
      align-items: center;
      min-height: 32px;
      padding: 5px 10px;
      color: var(--success);
      background: var(--success-soft);
      border: 1px solid #b8dfc7;
      border-radius: 999px;
      font-size: 13px;
      font-weight: 600;
      white-space: nowrap;
    }
    .status-pill[data-state="offline"] {
      color: var(--danger);
      background: var(--danger-soft);
      border-color: #f3c0ba;
    }
    main {
      width: min(1120px, calc(100% - 40px));
      margin: 28px auto 64px;
    }
    .page-heading { margin-bottom: 22px; }
    .page-heading h1 { margin: 0; font-size: 26px; line-height: 1.25; }
    .page-heading p { margin: 7px 0 0; color: var(--muted); }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(0, 1.08fr);
      gap: 20px;
      align-items: start;
    }
    .general-panel { grid-column: 1; grid-row: 1; }
    .companion-panel { grid-column: 1; grid-row: 2; }
    .speech-panel { grid-column: 1; grid-row: 3; }
    .devices-panel {
      grid-column: 2;
      grid-row: 1 / span 3;
      align-self: stretch;
    }
    .panel {
      overflow: hidden;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }
    .panel-header {
      padding: 18px 20px 14px;
      border-bottom: 1px solid var(--border);
    }
    .panel-header h2 { margin: 0; font-size: 18px; line-height: 1.35; }
    .panel-header p { margin: 5px 0 0; color: var(--muted); font-size: 14px; }
    .panel-body { padding: 20px; }
    .device-list { display: grid; gap: 0; }
    .device-filters {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 16px;
    }
    .filter-button {
      min-height: 38px;
      padding: 7px 12px;
      color: var(--muted);
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 999px;
      cursor: pointer;
      font-weight: 650;
    }
    .filter-button:hover { color: var(--text); border-color: var(--border-strong); }
    .filter-button.is-active {
      color: #fff;
      background: var(--accent);
      border-color: var(--accent);
    }
    .budget-row {
      display: flex;
      flex-direction: column;
      gap: 6px;
      margin-top: 8px;
    }
    .budget-input {
      width: 120px;
      box-sizing: border-box;
      padding: 6px 8px;
      border: 1px solid rgba(148, 163, 184, 0.35);
      border-radius: 6px;
      background: rgba(15, 23, 42, 0.6);
      color: inherit;
      font: inherit;
    }
    .device-card {
      min-width: 0;
      background: transparent;
    }
    .device-card + .device-card {
      margin-top: 18px;
      padding-top: 18px;
      border-top: 1px solid var(--border);
    }
    .device-title {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 14px;
      padding: 0 0 12px;
      border-bottom: 1px solid var(--border);
    }
    .device-title h3 { margin: 0; font-size: 16px; overflow-wrap: anywhere; }
    .device-meta { margin-top: 4px; color: var(--muted); font-size: 13px; overflow-wrap: anywhere; }
    .device-state {
      flex: 0 0 auto;
      padding: 3px 8px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      color: var(--success);
      background: var(--success-soft);
    }
    .device-state.offline { color: var(--muted); background: #edf0f2; }
    .device-state.pending { color: var(--warning); background: var(--warning-soft); }
    .device-state.rejected,
    .device-state.revoked { color: var(--danger); background: var(--danger-soft); }
    .device-kind {
      display: inline-block;
      margin-right: 6px;
      padding: 1px 6px;
      color: var(--muted);
      background: var(--surface-muted);
      border: 1px solid var(--border);
      border-radius: 4px;
      font-size: 11px;
      font-weight: 700;
      vertical-align: 1px;
    }
    .device-content { padding: 14px 0 0; }
    .subsection-title {
      margin: 0 0 9px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0;
      text-transform: uppercase;
    }
    .capability-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(145px, 1fr));
      gap: 8px;
    }
    .check-row {
      display: flex;
      align-items: center;
      gap: 9px;
      min-height: 40px;
      padding: 7px 9px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      cursor: pointer;
    }
    .check-row input { width: 18px; height: 18px; margin: 0; accent-color: var(--accent); }
    .device-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      margin-top: 14px;
    }
    .pairing-result {
      min-height: 42px;
      margin-top: 12px;
      padding: 10px 12px;
      color: var(--muted);
      background: var(--surface);
      border: 1px dashed var(--border-strong);
      border-radius: 6px;
      font-size: 13px;
      overflow-wrap: anywhere;
    }
    .pairing-result[data-state="success"] { color: var(--success); border-style: solid; border-color: #b8dfc7; background: var(--success-soft); }
    .pairing-result[data-state="error"] { color: var(--danger); border-style: solid; border-color: #f3c0ba; background: var(--danger-soft); }
    .token {
      display: block;
      margin-top: 4px;
      color: var(--text);
      font-family: Consolas, "Courier New", monospace;
      font-size: 13px;
      user-select: all;
    }
    .empty-state {
      padding: 28px 16px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed var(--border-strong);
      border-radius: var(--radius);
      background: var(--surface-muted);
    }
    .source-options {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin: 0 0 18px;
      padding: 0;
      border: 0;
    }
    .source-options legend { grid-column: 1 / -1; }
    .source-option {
      display: flex;
      align-items: flex-start;
      gap: 12px;
      min-height: 58px;
      padding: 12px 13px;
      border: 1px solid var(--border);
      border-radius: var(--radius);
      background: var(--surface-muted);
      cursor: pointer;
    }
    .source-option:has(input:checked) {
      border-color: var(--accent);
      background: var(--accent-soft);
      box-shadow: inset 3px 0 0 var(--accent);
    }
    .source-option input { margin-top: 4px; width: 18px; height: 18px; accent-color: var(--accent); }
    .source-option strong { display: block; font-size: 15px; }
    .source-option span { display: block; margin-top: 2px; color: var(--muted); font-size: 13px; }
    .remote-fields { display: grid; gap: 15px; }
    .remote-fields[hidden] { display: none; }
    .field { display: grid; gap: 6px; }
    .field label { font-size: 14px; font-weight: 650; }
    .field-hint { margin: 0; color: var(--muted); font-size: 13px; }
    .field-error { min-height: 18px; margin: 0; color: var(--danger); font-size: 13px; }
    .field input {
      width: 100%;
      min-height: 44px;
      padding: 9px 11px;
      color: var(--text);
      background: #fff;
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      outline: none;
    }
    .field input:hover { border-color: #94a1ad; }
    .field input:focus-visible,
    button:focus-visible,
    .source-option:focus-within {
      outline: 3px solid rgba(23, 107, 135, 0.24);
      outline-offset: 2px;
      border-color: var(--accent);
    }
    .field input[aria-invalid="true"] { border-color: var(--danger); }
    .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
    .key-row { display: flex; gap: 8px; align-items: stretch; }
    .key-row input { flex: 1 1 auto; }
    .button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 42px;
      padding: 8px 14px;
      border: 1px solid var(--border-strong);
      border-radius: 6px;
      color: var(--text);
      background: #fff;
      cursor: pointer;
      font-weight: 650;
      white-space: nowrap;
    }
    .button:hover { background: #f3f6f8; border-color: #94a1ad; }
    .button:active { transform: translateY(1px); }
    .button:disabled { cursor: not-allowed; opacity: 0.55; transform: none; }
    .button-primary { color: #fff; background: var(--accent); border-color: var(--accent); }
    .button-primary:hover { background: var(--accent-hover); border-color: var(--accent-hover); }
    .button-danger { color: var(--danger); background: #fff; border-color: #e4a9a2; }
    .button-danger:hover { background: var(--danger-soft); }
    .button-ghost { border-color: transparent; background: transparent; color: var(--accent); }
    .button-ghost:hover { background: var(--accent-soft); border-color: transparent; }
    .form-actions {
      display: flex;
      flex-wrap: wrap;
      gap: 9px;
      margin-top: 20px;
      padding-top: 18px;
      border-top: 1px solid var(--border);
    }
    .form-message {
      min-height: 44px;
      margin-top: 12px;
      padding: 11px 12px;
      color: var(--muted);
      background: var(--surface-muted);
      border: 1px solid var(--border);
      border-radius: 6px;
      font-size: 14px;
      overflow-wrap: anywhere;
    }
    .form-message[data-state="success"] { color: var(--success); background: var(--success-soft); border-color: #b8dfc7; }
    .form-message[data-state="error"] { color: var(--danger); background: var(--danger-soft); border-color: #f3c0ba; }
    .form-message[data-state="working"] { color: var(--warning); background: var(--warning-soft); border-color: #edd28b; }
    .note {
      margin: 16px 0 0;
      padding: 11px 12px;
      color: var(--muted);
      background: var(--surface-muted);
      border-left: 3px solid var(--border-strong);
      font-size: 13px;
    }
    .general-list {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }
    .general-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      min-height: 58px;
      padding: 12px 13px;
      background: var(--surface-muted);
      border: 1px solid var(--border);
      border-radius: var(--radius);
    }
    .general-row strong { display: block; font-size: 15px; }
    .general-row p { margin: 2px 0 0; color: var(--muted); font-size: 13px; }
    .switch {
      position: relative;
      flex: 0 0 auto;
      width: 48px;
      height: 28px;
      accent-color: var(--accent);
    }
    .general-actions { display: flex; flex-wrap: wrap; gap: 9px; margin-top: 14px; }
    .pair-code { margin: 14px 0 6px; padding: 14px 16px; border-radius: 12px; background: #101828; color: #f8fafc; font-size: 30px; font-weight: 700; letter-spacing: 6px; text-align: center; }
    .pair-code small { display: block; margin-top: 6px; font-size: 12px; font-weight: 400; letter-spacing: 0; color: #cbd5e1; }
    .port-list { display: grid; gap: 8px; margin-top: 8px; }
    .port-row {
      display: grid;
      grid-template-columns: minmax(140px, 1fr) minmax(96px, 120px) minmax(96px, auto);
      align-items: center;
      gap: 12px;
      padding: 10px 12px;
      border: 1px solid var(--border);
      border-radius: 8px;
      background: var(--surface);
    }
    .port-row strong { display: block; font-size: 14px; }
    .port-row p { margin: 2px 0 0; color: var(--muted); font-size: 12px; }
    .port-number { font-family: Consolas, "Courier New", monospace; font-weight: 700; }
    .port-edit { width: 100%; min-height: 36px; padding: 6px 8px; border: 1px solid var(--border-strong); border-radius: 6px; font-family: Consolas, "Courier New", monospace; font-weight: 700; }
    .port-row .button { min-height: 36px; padding: 6px 10px; }
    .port-actions-cell { display: flex; align-items: center; justify-content: flex-end; gap: 8px; min-width: 0; }
    .port-state {
      padding: 3px 8px;
      border-radius: 999px;
      color: var(--success);
      background: var(--success-soft);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .port-state[data-state="error"] { color: var(--danger); background: var(--danger-soft); }
    .port-state[data-state="disabled"] { color: var(--muted); background: #edf0f2; }
    .port-state[data-state="reserved"] { color: var(--warning); background: var(--warning-soft); }
    .port-actions { display: flex; align-items: end; gap: 8px; margin-top: 12px; }
    .port-actions .field { flex: 1 1 180px; }
    .port-actions input { width: 100%; min-height: 42px; padding: 8px 10px; border: 1px solid var(--border-strong); border-radius: 6px; }
    @media (max-width: 900px) {
      .layout {
        grid-template-columns: minmax(0, 1fr);
      }
      .general-panel { grid-column: 1; grid-row: auto; order: 1; }
      .companion-panel { grid-column: 1; grid-row: auto; order: 2; }
      .devices-panel { grid-column: 1; grid-row: auto; align-self: auto; order: 3; }
      .speech-panel { grid-column: 1; grid-row: auto; order: 4; }
    }
    @media (max-width: 560px) {
      .topbar-inner, main { width: min(100% - 24px, 1120px); }
      .topbar-inner { min-height: 60px; }
      .brand span { display: none; }
      main { margin-top: 20px; }
      .page-heading h1 { font-size: 23px; }
      .panel-header, .panel-body, .device-title, .device-content { padding-left: 14px; padding-right: 14px; }
      .device-title { padding-left: 0; padding-right: 0; }
      .device-content { padding-left: 0; padding-right: 0; }
      .general-list, .source-options { grid-template-columns: 1fr; }
      .field-row { grid-template-columns: 1fr; }
      .key-row { align-items: stretch; }
      .form-actions .button { flex: 1 1 145px; }
    }
    @media (prefers-reduced-motion: reduce) {
      *, *::before, *::after { scroll-behavior: auto !important; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand">
        <strong>CodexBridge</strong>
        <span>管理中心</span>
      </div>
      <div id="bridge-status" class="status-pill" role="status" aria-live="polite">正在连接本机服务</div>
    </div>
  </header>

  <main id="main-content">
    <div class="page-heading">
      <h1>设备与语音设置</h1>
      <p>连接与发现集中处理手机与硬件接入，设备中心统一查看状态、权限和注册动作。</p>
    </div>

    <div class="layout">
      <section class="panel general-panel" aria-labelledby="general-title">
        <div class="panel-header">
          <h2 id="general-title">常规设置</h2>
          <p>这些设置在下次启动或重新打开管理中心后仍然保留。</p>
        </div>
        <div class="panel-body">
          <div class="general-list">
            <div class="general-row">
              <div>
                <strong>开机自启动</strong>
                <p>登录 Windows 后自动运行 CodexBridge。</p>
              </div>
              <input id="startup-enabled" class="switch" type="checkbox" role="switch" aria-label="开机自启动">
            </div>
            <div class="general-row">
              <div>
                <strong>日志目录</strong>
                <p>打开存放运行日志、录音和设备配置的目录。</p>
              </div>
              <button id="open-logs" class="button" type="button">打开日志目录</button>
            </div>
          </div>
          <div class="general-actions">
            <button id="refresh-general" class="button" type="button">刷新状态</button>
          </div>
          <div id="general-message" class="form-message" role="status" aria-live="polite">正在读取常规设置...</div>
        </div>
      </section>

      <section class="panel companion-panel" aria-labelledby="companion-title">
        <div class="panel-header">
          <h2 id="companion-title">连接与发现</h2>
          <p>手机用配对码接入，硬件在同一局域网内主动注册并由这里批准。</p>
        </div>
        <div class="panel-body">
          <div class="general-list">
            <div class="general-row">
              <div>
                <strong>允许手机连接</strong>
                <p>只允许局域网访问，所有数据请求都需要配对令牌。</p>
              </div>
              <input id="companion-enabled" class="switch" type="checkbox" role="switch" aria-label="允许手机连接">
            </div>
            <div class="general-row">
              <div>
                <strong>显示中间事件</strong>
                <p>对手机和 ESP 等已配对设备生效。关闭时只返回对话和最终回答，隐藏思考、命令、文件变更、工具结果和上下文提示。</p>
              </div>
              <input id="companion-show-intermediate" class="switch" type="checkbox" role="switch" aria-label="显示中间事件">
            </div>
            <div class="general-row">
              <div>
                <strong>连接地址</strong>
                <p id="companion-addresses">正在读取...</p>
              </div>
            </div>
            <div class="general-row">
              <div>
                <strong>手机服务端口</strong>
                <p>修改后会立即切换到新端口，已配对手机仍会重新发现电脑。</p>
              </div>
              <input id="companion-port" class="port-input" type="number" min="1" max="65535" inputmode="numeric" aria-label="手机服务端口">
            </div>
            <div class="general-row">
              <div>
                <strong>配对码</strong>
                <p>在手机端输入，10 分钟内有效，使用一次后立即失效。</p>
              </div>
              <button id="companion-new-code" class="button" type="button">生成配对码</button>
            </div>
          </div>
          <div class="port-actions">
            <button id="companion-save-port" class="button button-primary" type="button">修改端口</button>
            <button id="companion-refresh-ports" class="button" type="button">刷新端口状态</button>
          </div>
          <div id="companion-code" class="pair-code" hidden></div>
          <div class="subsection-title">端口状态</div>
          <div id="companion-port-list" class="port-list" aria-live="polite">
            <div class="empty-state">正在读取...</div>
          </div>
          <div id="companion-message" class="form-message" role="status" aria-live="polite"></div>
        </div>
      </section>

      <section class="panel devices-panel" aria-labelledby="devices-title">
        <div class="panel-header">
          <h2 id="devices-title">设备中心</h2>
          <p>手机和硬件使用同一份设备记录；待批准硬件先在这里确认。</p>
        </div>
        <div class="panel-body">
          <div class="device-filters" role="tablist" aria-label="设备筛选">
            <button class="filter-button is-active" type="button" data-device-filter="all" aria-selected="true">全部</button>
            <button class="filter-button" type="button" data-device-filter="phone" aria-selected="false">手机</button>
            <button class="filter-button" type="button" data-device-filter="hardware" aria-selected="false">硬件</button>
            <button class="filter-button" type="button" data-device-filter="pending" aria-selected="false">待批准</button>
          </div>
          <div id="device-list" class="device-list" aria-live="polite">
            <div class="empty-state">正在读取设备...</div>
          </div>
        </div>
      </section>

      <section class="panel speech-panel" aria-labelledby="speech-title">
        <div class="panel-header">
          <h2 id="speech-title">语音识别</h2>
          <p>Dotii 和 AI Passport 共用这一套设备语音配置。</p>
        </div>
        <div class="panel-body">
          <form id="speech-form" novalidate>
            <fieldset class="source-options">
              <legend class="subsection-title">识别方式</legend>
              <label class="source-option">
                <input type="radio" name="source" value="default" checked>
                <span>
                  <strong>内置识别</strong>
                  <span>优先使用本机离线模型，失败时回退 Windows 语音识别。</span>
                </span>
              </label>
              <label class="source-option">
                <input type="radio" name="source" value="openai">
                <span>
                  <strong>OpenAI 兼容接口</strong>
                  <span>使用兼容 /v1/audio/transcriptions 的远程服务。</span>
                </span>
              </label>
            </fieldset>

            <div id="remote-fields" class="remote-fields" hidden>
              <input type="text" autocomplete="username" tabindex="-1" aria-hidden="true" style="position:fixed;left:-10000px;top:auto;width:1px;height:1px;overflow:hidden">
              <div class="field">
                <label for="speech-endpoint">接口地址</label>
                <input id="speech-endpoint" name="endpoint" type="url" inputmode="url" autocomplete="url" placeholder="https://example.com/v1/audio/transcriptions" aria-describedby="endpoint-hint endpoint-error">
                <p id="endpoint-hint" class="field-hint">填写完整的转写接口地址，必须以 http:// 或 https:// 开头。</p>
                <p id="endpoint-error" class="field-error" aria-live="polite"></p>
              </div>

              <div class="field">
                <label for="speech-api-key">API Key</label>
                <div class="key-row">
                  <input id="speech-api-key" name="api_key" type="password" autocomplete="new-password" aria-describedby="key-hint key-error">
                  <button id="toggle-key" class="button" type="button" aria-controls="speech-api-key">显示</button>
                </div>
                <p id="key-hint" class="field-hint">已保存的 Key 不会回传到网页；留空表示继续使用原来的 Key。</p>
                <p id="key-error" class="field-error" aria-live="polite"></p>
              </div>

              <div class="field-row">
                <div class="field">
                  <label for="speech-model">模型</label>
                  <input id="speech-model" name="model" type="text" value="whisper-1" autocomplete="off" aria-describedby="model-error">
                  <p id="model-error" class="field-error" aria-live="polite"></p>
                </div>
                <div class="field">
                  <label for="speech-language">语言</label>
                  <input id="speech-language" name="language" type="text" value="zh" autocomplete="off" aria-describedby="language-error">
                  <p id="language-error" class="field-error" aria-live="polite"></p>
                </div>
              </div>

              <label class="check-row" for="clear-api-key" style="width:max-content;background:#fff">
                <input id="clear-api-key" name="clear_api_key" type="checkbox">
                <span>清除已保存的 API Key</span>
              </label>
            </div>

            <div class="form-actions">
              <button id="save-speech" class="button button-primary" type="submit">保存语音设置</button>
              <button id="test-speech" class="button" type="button">测试连接</button>
            </div>
            <div id="speech-message" class="form-message" role="status" aria-live="polite">正在读取语音设置...</div>
          </form>
          <p class="note">远程接口失败时会自动回退内置识别，避免设备完全无法说话输入。</p>
        </div>
      </section>
    </div>
  </main>

  <script>
    "use strict";
    const $ = (selector, root = document) => root.querySelector(selector);
    const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
    const featureNames = {
      status: "状态",
      text: "文字消息",
      voice: "语音指令",
      approval: "审批",
      tasks: "任务",
      audio: "录音",
      read_ack: "已读同步"
    };

    async function requestJson(url, options = {}) {
      const response = await fetch(url, options);
      let payload;
      try {
        payload = await response.json();
      } catch (_error) {
        throw new Error("本机服务返回了无法解析的响应");
      }
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || "操作失败");
      }
      return payload;
    }

    function setMessage(element, text, state = "") {
      if (!element) return;
      element.textContent = text;
      if (state) element.dataset.state = state;
      else delete element.dataset.state;
    }

    function setBusy(button, busy, label = "处理中...") {
      if (!button) return;
      if (busy) {
        button.dataset.originalLabel = button.textContent;
        button.textContent = label;
        button.disabled = true;
      } else {
        button.textContent = button.dataset.originalLabel || button.textContent;
        button.disabled = false;
      }
    }

    async function loadStatus() {
      const element = $("#bridge-status");
      try {
        const payload = await requestJson("/api/admin/status");
        element.textContent = `服务正常 · v${payload.version} · ${payload.device_count} 台设备`;
        element.dataset.state = "online";
      } catch (error) {
        element.textContent = "无法连接本机服务";
        element.dataset.state = "offline";
      }
    }

    function appendText(parent, text, className) {
      const node = document.createElement("span");
      if (className) node.className = className;
      node.textContent = text;
      parent.appendChild(node);
      return node;
    }

    function createDeviceCard(device) {
      const card = document.createElement("article");
      card.className = "device-card";

      const title = document.createElement("div");
      title.className = "device-title";
      const titleText = document.createElement("div");
      const heading = document.createElement("h3");
      heading.textContent = device.name || device.device_id;
      titleText.appendChild(heading);
      const meta = document.createElement("div");
      meta.className = "device-meta";
      const kind = document.createElement("span");
      kind.className = "device-kind";
      kind.textContent = device.kind === "phone" ? "手机" : "硬件";
      meta.appendChild(kind);
      appendText(meta, `${device.model || "未知型号"} · ${device.device_id}`);
      if (device.last_address) appendText(meta, ` · ${device.last_address}`);
      titleText.appendChild(meta);
      title.appendChild(titleText);
      const state = document.createElement("span");
      const registrationState = device.registration_state || "approved";
      if (registrationState === "pending") {
        state.className = "device-state pending";
        state.textContent = "待批准";
      } else if (registrationState === "rejected" || registrationState === "revoked") {
        state.className = `device-state ${registrationState}`;
        state.textContent = registrationState === "rejected" ? "已拒绝" : "已撤销";
      } else {
        state.className = device.online ? "device-state" : "device-state offline";
        state.textContent = device.online ? "在线" : "离线";
      }
      title.appendChild(state);
      card.appendChild(title);

      const content = document.createElement("div");
      content.className = "device-content";
      const capabilityTitle = document.createElement("h4");
      capabilityTitle.className = "subsection-title";
      capabilityTitle.textContent = "可用能力";
      content.appendChild(capabilityTitle);

      const supported = Array.isArray(device.supported) ? device.supported : [];
      const editableHardware = device.kind === "hardware" && registrationState === "approved";
      if (supported.length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = "此设备还没有上报可用能力。";
        content.appendChild(empty);
      } else {
        const grid = document.createElement("div");
        grid.className = "capability-grid";
        const enabled = new Set(Array.isArray(device.enabled) ? device.enabled : []);
        supported.forEach((feature) => {
          const label = document.createElement("label");
          label.className = "check-row";
          const input = document.createElement("input");
          input.type = "checkbox";
          input.value = feature;
          input.checked = enabled.has(feature);
          input.disabled = !editableHardware;
          input.setAttribute("data-feature", feature);
          const text = document.createElement("span");
          text.textContent = featureNames[feature] || feature;
          label.appendChild(input);
          label.appendChild(text);
          grid.appendChild(label);
        });
        content.appendChild(grid);
      }

      const budgetTitle = document.createElement("h4");
      budgetTitle.className = "subsection-title";
      budgetTitle.textContent = "返回文本量";
      content.appendChild(budgetTitle);

      const budgetRow = document.createElement("div");
      budgetRow.className = "budget-row";
      const budgetLabel = document.createElement("label");
      budgetLabel.className = "check-row";
      budgetLabel.setAttribute("for", `budget-${device.device_id}`);
      budgetLabel.textContent = "每条正文最多字符数（留空表示不限，按设备声明）";
      const budgetInput = document.createElement("input");
      budgetInput.type = "number";
      budgetInput.min = "16";
      budgetInput.max = "100000";
      budgetInput.step = "1";
      budgetInput.id = `budget-${device.device_id}`;
      budgetInput.className = "budget-input";
      budgetInput.setAttribute("data-budget", "1");
      const declared = device.max_text_chars;
      budgetInput.value = typeof declared === "number" ? String(declared) : "";
      budgetInput.placeholder = typeof declared === "number" ? String(declared) : "不限";
      budgetInput.disabled = !editableHardware;
      budgetRow.appendChild(budgetLabel);
      budgetRow.appendChild(budgetInput);
      content.appendChild(budgetRow);

      const actions = document.createElement("div");
      actions.className = "device-actions";
      const result = document.createElement("div");
      result.className = "pairing-result";
      result.setAttribute("aria-live", "polite");

      if (registrationState === "pending") {
        result.textContent = "等待本机批准。";
        const approve = document.createElement("button");
        approve.type = "button";
        approve.className = "button button-primary";
        approve.textContent = "批准";
        approve.addEventListener("click", () => registrationAction(device, "approve", approve));
        actions.appendChild(approve);

        const reject = document.createElement("button");
        reject.type = "button";
        reject.className = "button button-danger";
        reject.textContent = "拒绝";
        reject.addEventListener("click", () => registrationAction(device, "reject", reject));
        actions.appendChild(reject);
      } else if (device.kind === "phone") {
        result.textContent = `配对方式：${device.registration_method === "pairing_code" ? "配对码" : "本机批准"}`;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "button button-danger";
        remove.textContent = "解除手机";
        remove.addEventListener("click", () => removePhone(device, remove));
        actions.appendChild(remove);
      } else if (registrationState === "approved") {
        result.textContent = device.has_token ? "设备凭据已登记。" : "设备缺少令牌，需要重新批准。";
        const save = document.createElement("button");
        save.type = "button";
        save.className = "button button-primary";
        save.textContent = "保存能力";
        save.addEventListener("click", () => saveCapabilities(device, card, save));
        actions.appendChild(save);

        const revoke = document.createElement("button");
        revoke.type = "button";
        revoke.className = "button button-danger";
        revoke.textContent = "撤销";
        revoke.addEventListener("click", () => registrationAction(device, "revoke", revoke));
        actions.appendChild(revoke);
      } else {
        result.textContent = registrationState === "rejected" ? "已拒绝，等待设备重新注册。" : "已撤销，等待设备重新注册。";
        const approve = document.createElement("button");
        approve.type = "button";
        approve.className = "button";
        approve.textContent = "重新批准";
        approve.addEventListener("click", () => registrationAction(device, "approve", approve));
        actions.appendChild(approve);
      }
      content.appendChild(actions);
      content.appendChild(result);

      card.appendChild(content);
      return card;
    }

    let deviceFilter = "all";

    function deviceMatchesFilter(device, filter) {
      if (filter === "all") return true;
      if (filter === "pending") return device.registration_state === "pending";
      return device.kind === filter;
    }

    async function loadDevices() {
      const list = $("#device-list");
      try {
        const payload = await requestJson("/api/admin/devices");
        list.textContent = "";
        const devices = Array.isArray(payload.devices) ? payload.devices : [];
        const visible = devices.filter((device) => deviceMatchesFilter(device, deviceFilter));
        if (visible.length === 0) {
          const empty = document.createElement("div");
          empty.className = "empty-state";
          empty.textContent = deviceFilter === "pending"
            ? "没有等待批准的硬件。"
            : "没有符合当前筛选的设备。";
          list.appendChild(empty);
          return;
        }
        visible.forEach((device) => list.appendChild(createDeviceCard(device)));
      } catch (error) {
        list.textContent = "";
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = error.message;
        list.appendChild(empty);
      }
    }

    $$("[data-device-filter]").forEach((button) => {
      button.addEventListener("click", () => {
        deviceFilter = button.dataset.deviceFilter || "all";
        $$("[data-device-filter]").forEach((candidate) => {
          const active = candidate === button;
          candidate.classList.toggle("is-active", active);
          candidate.setAttribute("aria-selected", active ? "true" : "false");
        });
        loadDevices();
      });
    });

    async function saveCapabilities(device, card, button) {
      const enabled = $$('input[type="checkbox"]', card).filter((input) => input.checked).map((input) => input.value);
      const budgetInput = $('input[data-budget="1"]', card);
      const rawBudget = budgetInput ? budgetInput.value.trim() : "";
      const body = {device_id: device.device_id, enabled};
      // Empty means unlimited; a value overrides the device-declared budget.
      body.max_text_chars = rawBudget === "" ? null : Number(rawBudget);
      setBusy(button, true, "保存中...");
      try {
        const result = await requestJson("/api/admin/devices/capabilities", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(body)
        });
        device.enabled = enabled;
        if (budgetInput && typeof result.max_text_chars !== "undefined") {
          device.max_text_chars = result.max_text_chars;
          budgetInput.placeholder = typeof result.max_text_chars === "number"
            ? String(result.max_text_chars)
            : "不限";
        }
        button.textContent = "已保存";
        window.setTimeout(() => { button.textContent = "保存能力"; }, 1400);
      } catch (error) {
        window.alert(`保存失败：${error.message}`);
      } finally {
        setBusy(button, false, "保存中...");
      }
    }

    async function removePhone(device, button) {
      if (!window.confirm(`解除手机“${device.name || device.device_id}”？`)) return;
      setBusy(button, true, "解除中...");
      try {
        await requestJson("/api/admin/companion", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "remove_phone", phone_id: device.device_id})
        });
        await Promise.all([loadDevices(), loadCompanion()]);
      } catch (error) {
        window.alert(`解除失败：${error.message}`);
        setBusy(button, false, "解除中...");
      }
    }

    async function registrationAction(device, action, button) {
      const labels = {approve: "批准", reject: "拒绝", revoke: "撤销"};
      const paths = {
        approve: "/api/admin/devices/approve",
        reject: "/api/admin/devices/reject",
        revoke: "/api/admin/devices/revoke"
      };
      const path = paths[action];
      if (!path) throw new Error("registration_action_invalid");
      if (action === "revoke" && !window.confirm(`撤销设备“${device.name || device.device_id}”？`)) return;
      setBusy(button, true, `${labels[action]}中...`);
      try {
        await requestJson(path, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({device_id: device.device_id})
        });
        await loadDevices();
      } catch (error) {
        window.alert(`${labels[action]}失败：${error.message}`);
        setBusy(button, false, `${labels[action]}中...`);
      }
    }

    function selectedSource() {
      const selected = $('input[name="source"]:checked', $("#speech-form"));
      return selected ? selected.value : "default";
    }

    function syncRemoteFields() {
      $("#remote-fields").hidden = selectedSource() !== "openai";
    }

    function speechPayload() {
      return {
        source: selectedSource(),
        endpoint: $("#speech-endpoint").value.trim(),
        api_key: $("#speech-api-key").value.trim(),
        model: $("#speech-model").value.trim() || "whisper-1",
        language: $("#speech-language").value.trim() || "zh",
        clear_api_key: $("#clear-api-key").checked
      };
    }

    function clearValidation() {
      ["speech-endpoint", "speech-api-key", "speech-model", "speech-language"].forEach((id) => {
        const input = $(`#${id}`);
        input.removeAttribute("aria-invalid");
        const error = $(`#${id.replace("speech-", "").replace("api-", "")}-error`) || $(`#${id}-error`);
        if (error) error.textContent = "";
      });
    }

    function validateSpeech() {
      clearValidation();
      const payload = speechPayload();
      if (payload.source !== "openai") return [];
      const errors = [];
      let valid = true;
      try {
        const url = new URL(payload.endpoint);
        if (!["http:", "https:"].includes(url.protocol) || !url.hostname || url.pathname === "/") {
          throw new Error("请输入包含 /v1/audio/transcriptions 路径的完整地址");
        }
      } catch (_error) {
        valid = false;
        errors.push("接口地址无效");
        $("#speech-endpoint").setAttribute("aria-invalid", "true");
        $("#endpoint-error").textContent = "请输入以 http:// 或 https:// 开头、包含接口路径的完整地址。";
      }
      if (!payload.model) {
        valid = false;
        errors.push("模型不能为空");
        $("#speech-model").setAttribute("aria-invalid", "true");
        $("#model-error").textContent = "请填写模型名称。";
      }
      return valid ? [] : errors;
    }

    async function loadSpeech() {
      try {
        const payload = await requestJson("/api/admin/settings/speech");
        const config = payload.speech;
        const radio = $(`input[name="source"][value="${config.source || "default"}"]`);
        if (radio) radio.checked = true;
        $("#speech-endpoint").value = config.endpoint || "";
        $("#speech-model").value = config.model || "whisper-1";
        $("#speech-language").value = config.language || "zh";
        $("#speech-api-key").value = "";
        $("#speech-api-key").placeholder = config.api_key_set ? "已保存，留空表示不修改" : "填写服务提供的 API Key";
        setMessage($("#speech-message"), config.api_key_set ? "已读取设置，API Key 已安全保存。" : "已读取设置。");
        syncRemoteFields();
      } catch (error) {
        setMessage($("#speech-message"), `读取失败：${error.message}`, "error");
      }
    }

    async function saveSpeech(event) {
      if (event) event.preventDefault();
      const errors = validateSpeech();
      if (errors.length) {
        setMessage($("#speech-message"), errors.join("；"), "error");
        return false;
      }
      const button = $("#save-speech");
      setBusy(button, true, "保存中...");
      setMessage($("#speech-message"), "正在保存语音设置...", "working");
      try {
        const payload = await requestJson("/api/admin/settings/speech", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(speechPayload())
        });
        $("#speech-api-key").value = "";
        $("#speech-api-key").placeholder = payload.speech.api_key_set ? "已保存，留空表示不修改" : "填写服务提供的 API Key";
        $("#clear-api-key").checked = false;
        setMessage($("#speech-message"), "语音设置已保存。", "success");
        return true;
      } catch (error) {
        setMessage($("#speech-message"), `保存失败：${error.message}`, "error");
        return false;
      } finally {
        setBusy(button, false, "保存中...");
      }
    }

    async function testSpeech() {
      const errors = validateSpeech();
      if (errors.length) {
        setMessage($("#speech-message"), errors.join("；"), "error");
        return;
      }
      const button = $("#test-speech");
      setBusy(button, true, "测试中...");
      setMessage($("#speech-message"), "正在请求远程接口，可能需要几秒钟...", "working");
      try {
        const payload = await requestJson("/api/admin/settings/speech/test", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(speechPayload())
        });
        setMessage($("#speech-message"), payload.message || "连接成功。", payload.ok ? "success" : "error");
      } catch (error) {
        setMessage($("#speech-message"), `连接失败：${error.message}`, "error");
      } finally {
        setBusy(button, false, "测试中...");
      }
    }

    async function loadGeneral() {
      const toggle = $("#startup-enabled");
      try {
        const payload = await requestJson("/api/admin/settings/general");
        toggle.checked = Boolean(payload.general.startup_enabled);
        setMessage($("#general-message"), "常规设置已读取。");
      } catch (error) {
        toggle.disabled = true;
        setMessage($("#general-message"), `读取失败：${error.message}`, "error");
      }
    }

    async function saveStartup() {
      const toggle = $("#startup-enabled");
      toggle.disabled = true;
      setMessage($("#general-message"), "正在保存开机自启动设置...", "working");
      try {
        const payload = await requestJson("/api/admin/settings/general", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({startup_enabled: toggle.checked})
        });
        toggle.checked = Boolean(payload.general.startup_enabled);
        setMessage(
          $("#general-message"),
          toggle.checked ? "已开启开机自启动。" : "已关闭开机自启动。",
          "success"
        );
      } catch (error) {
        setMessage($("#general-message"), `保存失败：${error.message}`, "error");
      } finally {
        toggle.disabled = false;
      }
    }

    async function openLogs() {
      const button = $("#open-logs");
      setBusy(button, true, "打开中...");
      try {
        await requestJson("/api/admin/settings/general", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "open_logs"})
        });
        setMessage($("#general-message"), "已打开日志目录。", "success");
      } catch (error) {
        setMessage($("#general-message"), `打开失败：${error.message}`, "error");
      } finally {
        setBusy(button, false, "打开中...");
      }
    }

    $("#startup-enabled").addEventListener("change", saveStartup);
    $("#open-logs").addEventListener("click", openLogs);
    $("#refresh-general").addEventListener("click", () => {
      $("#startup-enabled").disabled = false;
      loadGeneral();
    });

    $$('input[name="source"]').forEach((input) => input.addEventListener("change", syncRemoteFields));
    $("#speech-form").addEventListener("submit", saveSpeech);
    $("#test-speech").addEventListener("click", testSpeech);
    $("#toggle-key").addEventListener("click", () => {
      const input = $("#speech-api-key");
      const visible = input.type === "text";
      input.type = visible ? "password" : "text";
      $("#toggle-key").textContent = visible ? "显示" : "隐藏";
    });

    function companionTime(value) {
      if (!value) return "从未连接";
      const date = new Date(Number(value) * 1000);
      return Number.isNaN(date.getTime()) ? "从未连接" : `最近连接 ${date.toLocaleString()}`;
    }

    function portStateText(state) {
      return ({
        running: "正常",
        active: "正常",
        error: "异常",
        disabled: "已关闭",
        reserved: "已保留",
        fixed: "设备定义"
      })[state] || state || "未知";
    }

    function renderCompanionPorts(ports) {
      const list = $("#companion-port-list");
      list.textContent = "";
      if (!ports.length) {
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = "暂时没有端口状态。";
        list.appendChild(empty);
        return;
      }
      ports.forEach((item) => {
        const row = document.createElement("div");
        row.className = "port-row";
        const info = document.createElement("div");
        const title = document.createElement("strong");
        title.textContent = item.label || item.key;
        const detail = document.createElement("p");
        detail.textContent = `${item.protocol} · ${item.purpose || ""}`;
        info.appendChild(title);
        info.appendChild(detail);
        let number;
        if (item.editable) {
          number = document.createElement("input");
          number.className = "port-edit";
          number.type = "number";
          number.min = "1";
          number.max = "65535";
          number.inputMode = "numeric";
          number.value = String(item.port || "");
          number.setAttribute("aria-label", `${item.label || item.key}端口`);
        } else {
          number = document.createElement("span");
          number.className = "port-number";
          number.textContent = String(item.port || "--");
        }
        const state = document.createElement("span");
        state.className = "port-state";
        state.dataset.state = item.state || "";
        state.textContent = portStateText(item.state);
        const actions = document.createElement("div");
        actions.className = "port-actions-cell";
        row.appendChild(info);
        row.appendChild(number);
        if (item.editable) {
          const save = document.createElement("button");
          save.className = "button";
          save.type = "button";
          save.textContent = "保存";
          save.addEventListener("click", () => saveCompanionPort(item.key, number, save));
          actions.appendChild(save);
        }
        actions.appendChild(state);
        row.appendChild(actions);
        list.appendChild(row);
      });
    }

    async function loadCompanion() {
      const toggle = $("#companion-enabled");
      try {
        const payload = await requestJson("/api/admin/companion");
        const companion = payload.companion || {};
        toggle.checked = Boolean(companion.enabled);
        $("#companion-show-intermediate").checked = Boolean(companion.show_intermediate_events);
        const urls = companion.urls || [];
        $("#companion-addresses").textContent = urls.length ? urls.join(" · ") : "未检测到局域网地址";
        $("#companion-port").value = companion.port || "";
        renderCompanionPorts(companion.ports || []);
        if (!companion.enabled) {
          setMessage($("#companion-message"), "手机连接已关闭。");
        } else if (!$("#companion-message").dataset.state) {
          setMessage($("#companion-message"), "手机连接已开启。");
        }
      } catch (error) {
        toggle.disabled = true;
        setMessage($("#companion-message"), `读取失败：${error.message}`, "error");
      }
    }

    async function saveCompanionEnabled() {
      const toggle = $("#companion-enabled");
      toggle.disabled = true;
      setMessage($("#companion-message"), "正在保存手机连接设置...", "working");
      try {
        const payload = await requestJson("/api/admin/companion", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({enabled: toggle.checked})
        });
        const companion = payload.companion || {};
        toggle.checked = Boolean(companion.enabled);
        setMessage(
          $("#companion-message"),
          toggle.checked ? "已允许手机连接。" : "已关闭手机连接，已配对手机需要重新开启后再连接。",
          "success"
        );
        const urls = companion.urls || [];
        $("#companion-addresses").textContent = urls.length ? urls.join(" · ") : "未检测到局域网地址";
      } catch (error) {
        setMessage($("#companion-message"), `保存失败：${error.message}`, "error");
      } finally {
        toggle.disabled = false;
      }
    }

    async function saveCompanionIntermediateEvents() {
      const toggle = $("#companion-show-intermediate");
      toggle.disabled = true;
      setMessage($("#companion-message"), "正在保存中间事件设置...", "working");
      try {
        const payload = await requestJson("/api/admin/companion", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({show_intermediate_events: toggle.checked})
        });
        const companion = payload.companion || {};
        toggle.checked = Boolean(companion.show_intermediate_events);
        setMessage(
          $("#companion-message"),
          toggle.checked ? "已开启中间事件。" : "已关闭中间事件，只显示对话和最终回答。",
          "success"
        );
      } catch (error) {
        setMessage($("#companion-message"), `保存失败：${error.message}`, "error");
      } finally {
        toggle.disabled = false;
      }
    }

    async function newCompanionCode() {
      const button = $("#companion-new-code");
      setBusy(button, true, "生成中...");
      try {
        const payload = await requestJson("/api/admin/companion", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "new_code"})
        });
        const pairing = payload.pairing || {};
        const box = $("#companion-code");
        box.textContent = pairing.code || "------";
        const hint = document.createElement("small");
        hint.textContent = `有效期至 ${new Date(Number(pairing.expires_at) * 1000).toLocaleTimeString()}`;
        box.appendChild(hint);
        box.hidden = false;
        setMessage($("#companion-message"), "请在手机端输入配对码。", "success");
      } catch (error) {
        setMessage($("#companion-message"), `生成失败：${error.message}`, "error");
      } finally {
        setBusy(button, false, "生成中...");
      }
    }

    async function saveCompanionPort(kind = "companion", input = null, button = null) {
      input = input || $("#companion-port");
      button = button || $("#companion-save-port");
      const port = Number(input.value);
      if (!Number.isInteger(port) || port < 1 || port > 65535) {
        setMessage($("#companion-message"), "端口必须是 1 到 65535 之间的整数。", "error");
        return;
      }
      if (port === 8790) {
        setMessage($("#companion-message"), "8790 已保留给其他程序，CodexBridge 不能使用。", "error");
        return;
      }
      setBusy(button, true, "切换中...");
      setMessage($("#companion-message"), "正在验证并切换端口...", "working");
      try {
        const payload = await requestJson("/api/admin/companion", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({action: "set_port", kind, port})
        });
        const companion = payload.companion || {};
        $("#companion-port").value = companion.port || port;
        $("#companion-addresses").textContent = (companion.urls || []).join(" · ") || "未检测到局域网地址";
        renderCompanionPorts(companion.ports || []);
        const label = kind === "discovery" ? "手机发现端口" : "手机连接端口";
        setMessage($("#companion-message"), `${label}已切换为 ${port}。`, "success");
      } catch (error) {
        setMessage($("#companion-message"), `切换失败：${error.message}。原端口仍保持可用。`, "error");
      } finally {
        setBusy(button, false, "切换中...");
      }
    }

    $("#companion-enabled").addEventListener("change", saveCompanionEnabled);
    $("#companion-show-intermediate").addEventListener("change", saveCompanionIntermediateEvents);
    $("#companion-new-code").addEventListener("click", newCompanionCode);
    $("#companion-save-port").addEventListener("click", () => saveCompanionPort("companion"));
    $("#companion-refresh-ports").addEventListener("click", loadCompanion);

    loadStatus();
    loadGeneral();
    loadCompanion();
    loadDevices();
    loadSpeech();
  </script>
</body>
</html>
'''
