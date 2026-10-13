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
    .speech-panel { grid-column: 1; grid-row: 2; }
    .devices-panel {
      grid-column: 2;
      grid-row: 1 / span 2;
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
    @media (max-width: 900px) {
      .layout {
        grid-template-columns: minmax(0, 1fr);
      }
      .general-panel { grid-column: 1; grid-row: auto; order: 1; }
      .devices-panel { grid-column: 1; grid-row: auto; align-self: auto; order: 2; }
      .speech-panel { grid-column: 1; grid-row: auto; order: 3; }
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
      <p>这里统一管理接入设备、功能权限和语音识别来源。设置只对当前这台电脑生效。</p>
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

      <section class="panel devices-panel" aria-labelledby="devices-title">
        <div class="panel-header">
          <h2 id="devices-title">设备</h2>
          <p>设备只有在开启对应能力后，才会收到该类消息。</p>
        </div>
        <div class="panel-body">
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
      meta.textContent = `${device.model || "未知型号"} · ${device.device_id}`;
      titleText.appendChild(meta);
      title.appendChild(titleText);
      const state = document.createElement("span");
      state.className = device.online ? "device-state" : "device-state offline";
      state.textContent = device.online ? "在线" : "离线";
      title.appendChild(state);
      card.appendChild(title);

      const content = document.createElement("div");
      content.className = "device-content";
      const capabilityTitle = document.createElement("h4");
      capabilityTitle.className = "subsection-title";
      capabilityTitle.textContent = "可用能力";
      content.appendChild(capabilityTitle);

      const supported = Array.isArray(device.supported) ? device.supported : [];
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
          input.setAttribute("data-feature", feature);
          const text = document.createElement("span");
          text.textContent = featureNames[feature] || feature;
          label.appendChild(input);
          label.appendChild(text);
          grid.appendChild(label);
        });
        content.appendChild(grid);
      }

      const actions = document.createElement("div");
      actions.className = "device-actions";
      const save = document.createElement("button");
      save.type = "button";
      save.className = "button button-primary";
      save.textContent = "保存能力";
      save.addEventListener("click", () => saveCapabilities(device, card, save));
      actions.appendChild(save);

      const pairing = document.createElement("button");
      pairing.type = "button";
      pairing.className = "button";
      pairing.textContent = "生成配对令牌";
      pairing.addEventListener("click", () => loadPairing(device, result));
      actions.appendChild(pairing);

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "button button-danger";
      remove.textContent = "移除设备";
      remove.addEventListener("click", () => removeDevice(device, remove));
      actions.appendChild(remove);
      content.appendChild(actions);

      const result = document.createElement("div");
      result.className = "pairing-result";
      result.textContent = "尚未请求配对令牌。";
      result.setAttribute("aria-live", "polite");
      content.appendChild(result);

      card.appendChild(content);
      return card;
    }

    async function loadDevices() {
      const list = $("#device-list");
      try {
        const payload = await requestJson("/api/admin/devices");
        list.textContent = "";
        const devices = Array.isArray(payload.devices) ? payload.devices : [];
        if (devices.length === 0) {
          const empty = document.createElement("div");
          empty.className = "empty-state";
          empty.textContent = "还没有发现设备。请先让设备进入配对模式，然后保持在同一局域网。";
          list.appendChild(empty);
          return;
        }
        devices.forEach((device) => list.appendChild(createDeviceCard(device)));
      } catch (error) {
        list.textContent = "";
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = error.message;
        list.appendChild(empty);
      }
    }

    async function saveCapabilities(device, card, button) {
      const enabled = $$('input[type="checkbox"]', card).filter((input) => input.checked).map((input) => input.value);
      setBusy(button, true, "保存中...");
      try {
        await requestJson("/api/admin/devices/capabilities", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({device_id: device.device_id, enabled})
        });
        device.enabled = enabled;
        button.textContent = "已保存";
        window.setTimeout(() => { button.textContent = "保存能力"; }, 1400);
      } catch (error) {
        window.alert(`保存失败：${error.message}`);
      } finally {
        setBusy(button, false, "保存中...");
      }
    }

    async function loadPairing(device, result) {
      result.dataset.state = "working";
      result.textContent = "正在生成配对令牌...";
      try {
        const payload = await requestJson(`/api/admin/pairing?device_id=${encodeURIComponent(device.device_id)}`);
        result.textContent = `配对端口 ${payload.port}`;
        const token = document.createElement("code");
        token.className = "token";
        token.textContent = payload.token;
        result.appendChild(token);
        result.dataset.state = "success";
      } catch (error) {
        result.textContent = `生成失败：${error.message}`;
        result.dataset.state = "error";
      }
    }

    async function removeDevice(device, button) {
      if (!window.confirm(`移除设备“${device.name || device.device_id}”？`)) return;
      setBusy(button, true, "移除中...");
      try {
        await requestJson("/api/admin/devices/remove", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({device_id: device.device_id})
        });
        await loadDevices();
      } catch (error) {
        window.alert(`移除失败：${error.message}`);
        setBusy(button, false, "移除中...");
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

    loadStatus();
    loadGeneral();
    loadDevices();
    loadSpeech();
  </script>
</body>
</html>
'''
