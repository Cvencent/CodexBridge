from __future__ import annotations


# Interactive management page script.
#
# The page is served as one HTML response, so this module owns the behaviour
# layer. Pure helpers live in ``script.py`` (LOGIC_JS) and reach this script
# through ``window.__CB_LOGIC__``.
APP_JS = r"""
(function () {
  "use strict";

  const LOGIC = window.__CB_LOGIC__ || {};
  const mapDeviceToViewModel = LOGIC.mapDeviceToViewModel || function (value) { return value || {}; };
  const sortDevices = LOGIC.sortDevices || function (value) { return Array.isArray(value) ? value.slice() : []; };
  const filterDevices = LOGIC.filterDevices || function (value) { return Array.isArray(value) ? value.slice() : []; };
  const formatRelativeTime = LOGIC.formatRelativeTime || function () { return ""; };
  const formatAbsoluteTime = LOGIC.formatAbsoluteTime || function () { return ""; };
  const registrationLabel = LOGIC.registrationLabel || function (value) { return value || "未知"; };
  const featureName = LOGIC.featureName || function (value) { return String(value || ""); };
  const queueCanEdit = LOGIC.queueCanEdit || function () { return false; };
  const queueCanSteer = LOGIC.queueCanSteer || function () { return false; };
  const queueCanDelete = LOGIC.queueCanDelete || function () { return false; };
  const normalizeDeviceFilter = LOGIC.normalizeDeviceFilter || function (value) {
    return Object.assign({ status: "all", kind: "all", query: "", sort: "default" }, value || {});
  };
  const deviceCredentialLabel = LOGIC.deviceCredentialLabel || function (model) {
    return model.hasToken ? "已登记" : "缺少令牌";
  };

  const ROUTES = ["overview", "devices", "queue", "access", "speech", "preferences"];
  const DEVICE_FILTER_STORAGE_KEY = "codexbridge.deviceFilter";
  const API = {
    status: "/api/admin/status",
    codexStatus: "/api/admin/codex-status",
    devices: "/api/admin/devices",
    approve: "/api/admin/devices/approve",
    reject: "/api/admin/devices/reject",
    revoke: "/api/admin/devices/revoke",
    removeDevice: "/api/admin/devices/remove",
    capabilities: "/api/admin/devices/capabilities",
    companion: "/api/admin/companion",
    general: "/api/admin/settings/general",
    speech: "/api/admin/settings/speech",
    speechTest: "/api/admin/settings/speech/test",
    queue: "/api/admin/queues"
  };
  const SORT_LABELS = { default: "默认排序", name: "名称", last_seen: "最近在线", added: "添加时间" };
  const REGISTRATION_TONES = { approved: "approved", pending: "pending", rejected: "blocked", revoked: "blocked" };
  const ERROR_TEXT = {
    loopback_required: "管理接口只允许本机访问。",
    device_id_required: "缺少设备标识，请刷新页面后重试。",
    device_not_found: "设备记录不存在，可能已被移除。",
    registration_unavailable: "设备注册动作当前不可用。",
    registration_failed: "设备状态已变化，请刷新后重试。",
    enabled_required: "请先选择要启用的能力。",
    companion_unavailable: "手机接入服务当前不可用。",
    port_invalid: "端口必须是 1 到 65535 之间的整数。",
    port_reserved: "8790 已保留给其他程序，CodexBridge 不能使用。",
    port_in_use: "该端口已被占用或属于固定端口，请换一个端口。",
    port_kind: "端口类型无效。",
    show_intermediate_events: "中间事件设置无效。",
    enabled: "手机连接开关状态无效。",
    invalid_request: "请求内容无效。",
    json_content_type_required: "请求格式无效。",
    body_too_large: "请求内容过大。",
    not_found: "接口不存在或已被移除。",
    startup_enabled_required: "缺少开机自启动设置。",
    startup_enabled_invalid: "开机自启动设置无效。",
    network_unreachable: "无法连接本机服务，请确认 CodexBridge 正在运行。",
    invalid_response: "本机服务返回了无法解析的响应。"
  };

  const state = {
    route: "overview",
    status: null,
    codex: null,
    devices: [],
    deviceFilter: normalizeDeviceFilter(readStoredDeviceFilter()),
    selectedDeviceId: null,
    drawerTrigger: null,
    drawerDirty: false,
    queueFocus: null,
    queue: { enabled: false, threads: [], checked: false },
    companion: null,
    general: null,
    speech: null,
    pairing: null,
    errors: [],
    busy: {},
    lastUpdatedAt: 0,
    pendingFocus: false
  };

  function readStoredDeviceFilter() {
    try {
      const raw = window.sessionStorage.getItem(DEVICE_FILTER_STORAGE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (_error) {
      return null;
    }
  }

  function persistDeviceFilter(value) {
    state.deviceFilter = normalizeDeviceFilter(value);
    try {
      window.sessionStorage.setItem(DEVICE_FILTER_STORAGE_KEY, JSON.stringify(state.deviceFilter));
    } catch (_error) {
      // Storage may be unavailable in privacy-restricted contexts. The filter
      // still remains valid for this page instance.
    }
  }

  function syncDeviceFilterControls() {
    const status = $("#device-status-filter");
    if (status) status.value = state.deviceFilter.status;
    const kind = $("#device-kind-filter");
    if (kind) kind.value = state.deviceFilter.kind;
    const sort = $("#device-sort");
    if (sort) sort.value = state.deviceFilter.sort;
    const query = $("#device-search");
    if (query) query.value = state.deviceFilter.query;
  }

  function $(selector, root) {
    return (root || document).querySelector(selector);
  }

  function $$(selector, root) {
    return Array.prototype.slice.call((root || document).querySelectorAll(selector));
  }

  function clearNode(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function build(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (typeof text === "string") node.textContent = text;
    return node;
  }

  function icon(name, className) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", className || "icon");
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#icon-" + name);
    svg.appendChild(use);
    return svg;
  }

  function userMessage(error) {
    if (!error) return "操作失败，请稍后重试。";
    if (error.code && ERROR_TEXT[error.code]) return ERROR_TEXT[error.code];
    return error.message || "操作失败，请稍后重试。";
  }

  async function api(path, options) {
    const settings = options || {};
    const request = { method: settings.method || "GET", headers: {} };
    if (settings.body !== undefined) {
      request.headers["Content-Type"] = "application/json";
      request.body = JSON.stringify(settings.body);
    }
    let response;
    try {
      response = await fetch(path, request);
    } catch (_cause) {
      const failure = new Error(ERROR_TEXT.network_unreachable);
      failure.code = "network_unreachable";
      failure.retryable = true;
      throw failure;
    }
    const raw = await response.text();
    let payload = null;
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch (_error) {
        const failure = new Error(ERROR_TEXT.invalid_response);
        failure.code = "invalid_response";
        throw failure;
      }
    }
    if (!response.ok || (payload && payload.ok === false)) {
      const code = (payload && (payload.error || payload.code)) || "http_" + response.status;
      const failure = new Error(
        (payload && payload.message) || ERROR_TEXT[code] || "操作失败，请稍后重试。"
      );
      failure.code = code;
      failure.retryable = response.status >= 500;
      failure.field = payload && payload.field;
      throw failure;
    }
    return payload || {};
  }

  function setBusy(button, busy, label) {
    if (!button) return;
    if (busy) {
      if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent;
      if (label) button.textContent = label;
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    } else {
      if (button.dataset.idleLabel) button.textContent = button.dataset.idleLabel;
      button.disabled = false;
      button.removeAttribute("aria-busy");
    }
  }

  function setAlert(node, text, tone) {
    if (!node) return;
    node.textContent = text || "";
    if (tone) node.dataset.state = tone;
    else node.removeAttribute("data-state");
    node.hidden = !text;
  }

  function toast(message, tone) {
    const region = $("#toast-region");
    if (!region) return;
    const node = build("div", "toast", message);
    node.dataset.state = tone || "info";
    region.appendChild(node);
    window.setTimeout(function () {
      if (node.parentNode) node.parentNode.removeChild(node);
    }, 4200);
  }

  let confirmResolver = null;

  function confirmAction(options) {
    const dialog = $("#confirm-dialog");
    if (!dialog || typeof dialog.showModal !== "function") {
      return Promise.resolve(true);
    }
    $("#confirm-title").textContent = (options && options.title) || "确认操作";
    $("#confirm-message").textContent = (options && options.message) || "";
    const ok = $("#confirm-ok");
    ok.textContent = (options && options.confirmLabel) || "确认";
    ok.className = "button " + ((options && options.danger === false) ? "button-primary" : "button-danger");
    dialog.returnValue = "";
    dialog.showModal();
    ok.focus();
    return new Promise(function (resolve) {
      confirmResolver = resolve;
    });
  }

  function settleConfirm(value) {
    const dialog = $("#confirm-dialog");
    if (dialog && dialog.open) dialog.close();
    const resolver = confirmResolver;
    confirmResolver = null;
    if (resolver) resolver(value);
  }

  function parseHash() {
    const raw = String(window.location.hash || "").replace(/^#/, "");
    const parts = raw.split("/");
    const route = ROUTES.indexOf(parts[0]) >= 0 ? parts[0] : "overview";
    const deviceId = parts.length > 1 ? decodeURIComponent(parts.slice(1).join("/")) : "";
    return { route: route, deviceId: deviceId };
  }

  function setRoute(route, options) {
    const settings = options || {};
    const target = ROUTES.indexOf(route) >= 0 ? route : "overview";
    const hash = settings.deviceId
      ? "#" + target + "/" + encodeURIComponent(settings.deviceId)
      : "#" + target;
    if (window.location.hash !== hash) {
      if (settings.push) history.pushState(null, "", hash);
      else history.replaceState(null, "", hash);
    }
    applyRoute();
  }

  function applyRoute() {
    const current = parseHash();
    let route = current.route;
    if (route === "queue" && !state.queue.enabled) {
      if (!state.queue.checked) {
        // Queue support is still being probed; hold the shell instead of
        // flashing an empty queue page, then re-evaluate.
        showPanel(state.route === "queue" ? "overview" : state.route);
        window.setTimeout(applyRoute, 500);
        return;
      }
      route = "overview";
      if (window.location.hash !== "#overview") {
        history.replaceState(null, "", "#overview");
      }
    }
    state.route = route;
    showPanel(route);
    if (current.deviceId && route === "devices") {
      if (state.selectedDeviceId !== current.deviceId) {
        openDrawer(current.deviceId, null, { silent: true });
      }
    } else if (state.selectedDeviceId) {
      closeDrawer({ force: true, silent: true });
    }
  }

  function showPanel(route) {
    ROUTES.forEach(function (name) {
      const panel = document.getElementById(name + "-panel");
      if (panel) panel.hidden = name !== route;
      const link = document.getElementById(name + "-nav-item");
      if (link) {
        if (name === route) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
      }
    });
  }

  function deviceById(deviceId) {
    return state.devices.find(function (model) { return model.deviceId === deviceId; }) || null;
  }

  function relativeTime(timestamp) {
    return formatRelativeTime(Math.floor(Date.now() / 1000), timestamp);
  }

  function absoluteTime(timestamp) {
    return formatAbsoluteTime(timestamp);
  }

  function statusTone(model) {
    return REGISTRATION_TONES[model.registrationState] || "approved";
  }

  function secondaryStatusText(model) {
    if (model.registrationState === "pending") return "尚未接入";
    if (model.registrationState === "rejected") return "等待重新申请";
    if (model.registrationState === "revoked") return "已阻止接入";
    if (model.online) return "在线";
    return model.lastSeen ? "离线 · " + relativeTime(model.lastSeen) : "离线";
  }

  function statusCell(model) {
    const stack = build("div", "state-stack");
    const label = build("span", "status-label " + statusTone(model), registrationLabel(model.registrationState));
    const line = build("span", "online-line" + (model.online ? " online" : ""), secondaryStatusText(model));
    if (model.lastSeen) line.title = absoluteTime(model.lastSeen);
    stack.appendChild(label);
    stack.appendChild(line);
    return stack;
  }

  function capabilitySummary(model) {
    const wrap = build("div", "capability-count");
    if (model.registrationState === "pending") {
      wrap.appendChild(build("span", null, String(model.supported.length)));
      wrap.appendChild(build("small", null, " 项待授权"));
      return wrap;
    }
    wrap.appendChild(build("span", null, String(model.enabled.length)));
    wrap.appendChild(build("small", null, " / " + model.supported.length));
    return wrap;
  }

  function queueBadge(model) {
    if (!state.queue.enabled || !model.queuedCount) return null;
    const badge = build("button", "queue-badge", "队列 " + model.queuedCount);
    badge.type = "button";
    badge.setAttribute("data-action", "open-queue");
    badge.setAttribute("data-device-id", model.deviceId);
    badge.setAttribute("title", "查看该设备的活动线程队列");
    return badge;
  }

  function deviceIdentity(model) {
    const identity = build("div", "device-identity");
    identity.appendChild(build("strong", null, model.name));
    identity.appendChild(build("span", null, (model.kind === "phone" ? "手机" : "硬件") + " · " + model.model));
    const id = build("code", null, model.deviceId);
    id.title = model.deviceId;
    identity.appendChild(id);
    if (model.lastAddress) {
      const address = build("span", "muted", model.lastAddress);
      address.title = model.lastAddress;
      identity.appendChild(address);
    }
    return identity;
  }

  function actionButton(label, action, deviceId, className) {
    const button = build("button", "button " + (className || ""), label);
    button.type = "button";
    button.setAttribute("data-action", action);
    button.setAttribute("data-device-id", deviceId);
    return button;
  }

  function rowActions(model) {
    const wrap = build("div", "row-actions");
    if (model.registrationState === "pending") {
      wrap.appendChild(actionButton("批准", "approve", model.deviceId, "button-primary"));
      wrap.appendChild(actionButton("拒绝", "reject", model.deviceId, "button-danger"));
      wrap.appendChild(actionButton("详情", "open-drawer", model.deviceId));
    } else if (model.registrationState === "approved" && model.kind === "hardware") {
      wrap.appendChild(actionButton("详情", "open-drawer", model.deviceId));
      wrap.appendChild(actionButton("保存能力", "quick-save", model.deviceId, "button-primary"));
      wrap.appendChild(actionButton("删除设备", "remove-hardware", model.deviceId, "button-danger"));
    } else if (model.registrationState === "approved") {
      wrap.appendChild(actionButton("详情", "open-drawer", model.deviceId));
      wrap.appendChild(actionButton("解除手机", "remove-phone", model.deviceId, "button-danger"));
    } else {
      wrap.appendChild(actionButton("重新批准", "approve", model.deviceId, "button-primary"));
      wrap.appendChild(actionButton("详情", "open-drawer", model.deviceId));
    }
    return wrap;
  }

  function renderDeviceTable() {
    const body = $("#device-table-body");
    const cards = $("#device-card-list");
    if (!body || !cards) return;
    const visible = sortDevices(filterDevices(state.devices, state.deviceFilter));
    const ordered = sortOrder(visible, state.deviceFilter.sort);
    clearNode(body);
    clearNode(cards);

    const summary = $("#device-result-summary");
    if (summary) {
      summary.textContent = state.devices.length
        ? "共 " + state.devices.length + " 台设备，当前显示 " + ordered.length + " 台 · " + SORT_LABELS[state.deviceFilter.sort]
        : "正在读取设备...";
    }
    const navCount = $("#devices-nav-count");
    if (navCount) {
      navCount.textContent = String(state.devices.length);
      navCount.hidden = state.devices.length === 0;
    }

    if (ordered.length === 0) {
      const empty = state.devices.length === 0 ? emptyState("system") : emptyState("filter");
      const cell = build("td");
      cell.colSpan = 5;
      cell.appendChild(empty);
      const row = build("tr");
      row.appendChild(cell);
      body.appendChild(row);
      cards.appendChild(emptyState(state.devices.length === 0 ? "system" : "filter"));
      return;
    }

    ordered.forEach(function (model) {
      body.appendChild(deviceRow(model));
      cards.appendChild(deviceCard(model));
    });

    if (state.pendingFocus) {
      state.pendingFocus = false;
      const first = $('[data-action="approve"][data-device-id]', body);
      if (first) {
        first.focus();
        return;
      }
    }
  }

  function sortOrder(devices, mode) {
    if (mode === "name") {
      return devices.slice().sort(function (a, b) { return a.name.localeCompare(b.name, "zh-Hans-CN"); });
    }
    if (mode === "last_seen") {
      return devices.slice().sort(function (a, b) { return (b.lastSeen || 0) - (a.lastSeen || 0); });
    }
    if (mode === "added") {
      return devices.slice().sort(function (a, b) { return (b.addedAt || 0) - (a.addedAt || 0); });
    }
    return devices;
  }

  function emptyState(kind) {
    const box = build("div", "empty-state");
    if (kind === "none") {
      box.appendChild(build("strong", null, "当前没有错误"));
      box.appendChild(build("p", null, "服务恢复正常后会在这里保留恢复入口。"));
      return box;
    }
    if (kind === "system") {
      box.appendChild(build("strong", null, "还没有已登记设备"));
      box.appendChild(build("p", null, "生成手机配对码，或在设备端发起注册申请。"));
      const action = build("button", "button button-primary", "生成手机配对码");
      action.type = "button";
      action.setAttribute("data-action", "new-code");
      action.style.marginTop = "10px";
      box.appendChild(action);
    } else if (kind === "pending") {
      box.appendChild(build("strong", null, "当前没有待批准设备"));
      box.appendChild(build("p", null, "新设备发起注册后会显示在这里。"));
    } else {
      box.appendChild(build("strong", null, "没有符合当前筛选的设备"));
      box.appendChild(build("p", null, "调整状态、类型或搜索条件后重试。"));
      const action = build("button", "button", "清除筛选");
      action.type = "button";
      action.setAttribute("data-action", "clear-filter");
      action.style.marginTop = "10px";
      box.appendChild(action);
    }
    return box;
  }

  function deviceRow(model) {
    const row = build("tr");
    row.setAttribute("data-device-id", model.deviceId);

    const stateCell = build("td");
    stateCell.appendChild(statusCell(model));
    row.appendChild(stateCell);

    const deviceCell = build("td");
    deviceCell.appendChild(deviceIdentity(model));
    row.appendChild(deviceCell);

    const capabilityCell = build("td");
    capabilityCell.appendChild(capabilitySummary(model));
    row.appendChild(capabilityCell);

    const seenCell = build("td");
    const seen = build("span", "tooltip-time", model.lastSeen ? relativeTime(model.lastSeen) : "从未");
    seen.title = absoluteTime(model.lastSeen);
    seenCell.appendChild(seen);
    row.appendChild(seenCell);

    const actionCell = build("td");
    actionCell.appendChild(rowActions(model));
    row.appendChild(actionCell);
    return row;
  }

  function deviceCard(model) {
    const card = build("article", "device-card");
    card.setAttribute("data-device-id", model.deviceId);
    const head = build("div", "device-card-head");
    const title = build("div", "device-card-title");
    title.appendChild(build("strong", null, model.name));
    title.appendChild(build("span", null, (model.kind === "phone" ? "手机" : "硬件") + " · " + model.model));
    head.appendChild(title);
    head.appendChild(statusCell(model));
    card.appendChild(head);

    const meta = build("div", "device-card-meta");
    meta.appendChild(build("span", null, model.online ? "在线" : "离线"));
    meta.appendChild(build("span", null, model.lastSeen ? "最近 " + relativeTime(model.lastSeen) : "从未连接"));
    if (model.lastAddress) meta.appendChild(build("span", "muted", model.lastAddress));
    const badge = queueBadge(model);
    if (badge) meta.appendChild(badge);
    card.appendChild(meta);

    const capability = build("div", "device-card-meta");
    capability.appendChild(capabilitySummary(model));
    card.appendChild(capability);

    const actions = build("div", "device-card-actions");
    actions.appendChild(rowActions(model));
    card.appendChild(actions);
    return card;
  }

  function renderOverview() {
    const status = state.status || {};
    const setText = function (id, text) {
      const node = document.getElementById(id);
      if (node) node.textContent = text;
    };
    setText("overview-service", status.ok === false ? "异常" : "正常");
    setText("overview-version", "版本 " + (status.version || "--"));
    const online = state.devices.filter(function (model) { return model.online; }).length;
    setText("overview-online", String(online) + " / " + state.devices.length);
    setText("overview-online-detail", "共 " + state.devices.length + " 台设备");
    const pending = state.devices.filter(function (model) { return model.registrationState === "pending"; });
    setText("overview-pending", String(pending.length));
    const codex = state.codex || {};
    const codexEnabled = codex.enabled !== false;
    setText("overview-codex", codexEnabled ? "可用" : "未连接");
    setText("overview-codex-detail", codex.collector_detail || codex.collector_state || "本机 Codex 采集状态");

    const summary = {
      approved: state.devices.filter(function (model) { return model.registrationState === "approved"; }).length,
      online: online,
      offline: state.devices.filter(function (model) { return model.registrationState === "approved" && !model.online; }).length,
      blocked: state.devices.filter(function (model) {
        return model.registrationState === "rejected" || model.registrationState === "revoked";
      }).length
    };
    setText("summary-approved", String(summary.approved));
    setText("summary-online", String(summary.online));
    setText("summary-offline", String(summary.offline));
    setText("summary-blocked", String(summary.blocked));

    renderPendingList(pending);
    renderOverviewErrors();
    renderQueueSummary();
  }

  function renderPendingList(pending) {
    const list = $("#overview-pending-list");
    if (!list) return;
    clearNode(list);
    if (!pending.length) {
      list.appendChild(emptyState("pending"));
      return;
    }
    pending.forEach(function (model) {
      const row = build("div", "summary-row");
      const info = build("div", "device-identity");
      info.appendChild(build("strong", null, model.name));
      info.appendChild(build("span", null, model.model + " · " + model.supported.length + " 项请求能力"));
      info.appendChild(build("span", "muted", model.lastSeen ? "申请于 " + absoluteTime(model.lastSeen) : "等待批准"));
      row.appendChild(info);
      const actions = build("div", "row-actions");
      actions.appendChild(actionButton("批准", "approve", model.deviceId, "button-primary"));
      actions.appendChild(actionButton("拒绝", "reject", model.deviceId, "button-danger"));
      actions.appendChild(actionButton("查看详情", "open-drawer", model.deviceId));
      row.appendChild(actions);
      list.appendChild(row);
    });
  }

  function renderOverviewErrors() {
    const list = $("#overview-error-list");
    if (!list) return;
    clearNode(list);
    const errors = state.errors.slice();
    if (!errors.length) {
      list.appendChild(emptyState("none"));
      return;
    }
    errors.forEach(function (error) {
      const row = build("div", "summary-row");
      row.appendChild(build("span", null, userMessage(error)));
      const retry = build("button", "button", "重试");
      retry.type = "button";
      retry.setAttribute("data-action", "retry-all");
      row.appendChild(retry);
      list.appendChild(row);
    });
  }

  function renderQueueSummary() {
    const surface = $("#overview-queue-surface");
    if (!surface) return;
    surface.hidden = !state.queue.enabled;
    if (!state.queue.enabled) return;
    const summary = $("#overview-queue-summary");
    if (!summary) return;
    clearNode(summary);
    const total = state.queue.threads.reduce(function (count, thread) {
      return count + (thread.items ? thread.items.length : 0);
    }, 0);
    summary.appendChild(build("p", "muted", total ? "当前共 " + total + " 项待处理。" : "当前没有待发送项。"));
  }

  function updateStatusBar() {
    const node = $("#bridge-status");
    if (!node) return;
    if (state.status) {
      const online = state.devices.filter(function (model) { return model.online; }).length;
      node.textContent = "服务正常 · v" + (state.status.version || "--") + " · " + online + "/" + state.devices.length + " 台设备在线";
      node.dataset.state = "online";
    } else if (state.errors.length) {
      node.textContent = "无法连接本机服务";
      node.dataset.state = "offline";
    } else {
      node.textContent = "正在连接本机服务";
      node.removeAttribute("data-state");
    }
  }

  function updatePendingAlert() {
    const button = $("#pending-alert");
    if (!button) return;
    const pending = state.devices.filter(function (model) { return model.registrationState === "pending"; }).length;
    button.hidden = pending === 0;
    const count = $("#pending-alert-count");
    if (count) count.textContent = pending + " 台设备待批准";
  }

  function showGlobalError(error) {
    const banner = $("#global-error");
    const message = $("#global-error-message");
    if (!banner) return;
    if (!error) {
      banner.hidden = true;
      return;
    }
    banner.hidden = false;
    if (message) message.textContent = userMessage(error);
  }

  function openDrawer(deviceId, trigger, options) {
    const settings = options || {};
    const model = deviceById(deviceId);
    if (!model) {
      if (!settings.silent) toast("设备记录不存在，可能已被移除。", "error");
      return;
    }
    state.selectedDeviceId = deviceId;
    if (trigger) state.drawerTrigger = trigger;
    state.drawerDirty = false;
    renderDrawer(model);
    const drawer = $("#device-drawer");
    const backdrop = $("#drawer-backdrop");
    if (drawer) {
      drawer.hidden = false;
      document.body.classList.add("drawer-open");
    }
    if (backdrop) backdrop.hidden = false;
    if (!settings.silent) {
      const title = $("#drawer-title");
      if (title) title.focus();
    }
    if (state.route !== "devices" || parseHash().deviceId !== deviceId) {
      setRoute("devices", { deviceId: deviceId, push: true });
    }
  }

  function closeDrawer(options) {
    const settings = options || {};
    const drawer = $("#device-drawer");
    const backdrop = $("#drawer-backdrop");
    if (drawer) drawer.hidden = true;
    if (backdrop) backdrop.hidden = true;
    document.body.classList.remove("drawer-open");
    state.selectedDeviceId = null;
    state.drawerDirty = false;
    const trigger = state.drawerTrigger;
    state.drawerTrigger = null;
    if (!settings.silent && settings.restoreFocus !== false && trigger && document.contains(trigger)) {
      trigger.focus();
    }
    if (!settings.silent && parseHash().deviceId) {
      setRoute("devices");
    }
  }

  function renderDrawer(model) {
    const title = $("#drawer-title");
    if (title) title.textContent = model.name;
    const subtitle = $("#drawer-subtitle");
    if (subtitle) subtitle.textContent = (model.kind === "phone" ? "手机" : "硬件") + " · " + model.model;
    const stateBox = $("#drawer-state");
    if (stateBox) {
      clearNode(stateBox);
      stateBox.appendChild(statusCell(model));
    }
    const identity = $("#drawer-identity");
    if (identity) {
      clearNode(identity);
      facts(identity, [
        ["类型", model.kind === "phone" ? "手机" : "硬件"],
        ["型号", model.model],
        ["设备 ID", model.deviceId, "mono"],
        ["注册方式", model.registrationMethod === "pairing_code" ? "配对码" : "本机批准"]
      ]);
    }
    const connection = $("#drawer-connection");
    if (connection) {
      clearNode(connection);
      const rows = [
        ["最近在线", absoluteTime(model.lastSeen)],
        ["最近地址", model.lastAddress || "尚未记录"]
      ];
      rows.push(["凭据", deviceCredentialLabel(model)]);
      if (model.protocolVersion) rows.push(["协议版本", model.protocolVersion]);
      if (model.firmware) rows.push(["固件 / 客户端", model.firmware]);
      facts(connection, rows);
    }
    renderDrawerCapabilities(model);
    renderDrawerDanger(model);
    const queueSection = $("#drawer-queue-section");
    if (queueSection) {
      const enabled = state.queue.enabled && model.queuedCount > 0;
      queueSection.hidden = !enabled;
    }
    const queueCount = $("#drawer-queue-count");
    if (queueCount) queueCount.textContent = model.queuedCount + " 项";
    const footer = $(".drawer-footer");
    if (footer) {
      const save = $('[data-action="drawer-save"]', footer);
      if (save) save.hidden = !(model.kind === "hardware" && model.registrationState === "approved");
      const reject = $('[data-action="drawer-reject"]', footer);
      if (reject) reject.hidden = model.registrationState !== "pending";
      const revoke = $('[data-action="drawer-revoke"]', footer);
      if (revoke) revoke.hidden = !(model.kind === "hardware" && model.registrationState === "approved");
      const removeHardware = $('[data-action="drawer-remove-hardware"]', footer);
      if (removeHardware) removeHardware.hidden = !(model.kind === "hardware" && model.registrationState === "approved");
      const remove = $('[data-action="drawer-remove-phone"]', footer);
      if (remove) remove.hidden = !(model.kind === "phone" && model.registrationState === "approved");
    }
  }

  function facts(list, rows) {
    rows.forEach(function (row) {
      list.appendChild(build("dt", null, row[0]));
      const value = build("dd", row[2] || null, String(row[1]));
      if (row[2] === "mono") value.className = "mono";
      list.appendChild(value);
    });
  }

  function renderDrawerCapabilities(model) {
    const grid = $("#drawer-capabilities");
    if (!grid) return;
    clearNode(grid);
    const editable = model.kind === "hardware" && model.registrationState === "approved";
    if (!model.supported.length) {
      grid.appendChild(build("p", "muted", "此设备还没有上报可用能力。"));
    } else {
      const enabled = new Set(model.enabled);
      model.supported.forEach(function (feature) {
        const label = build("label", "check-row");
        const input = build("input");
        input.type = "checkbox";
        input.value = feature;
        input.checked = enabled.has(feature);
        input.disabled = !editable;
        input.setAttribute("data-capability", feature);
        label.appendChild(input);
        label.appendChild(build("span", null, featureName(feature)));
        grid.appendChild(label);
      });
    }
    const budget = $("#drawer-budget");
    if (budget) {
      budget.value = typeof model.maxTextChars === "number" ? String(model.maxTextChars) : "";
      budget.placeholder = typeof model.maxTextChars === "number" ? String(model.maxTextChars) : "不限";
      budget.disabled = !editable;
    }
    setAlert($("#drawer-message"), editable ? "硬件能力可编辑并保存。" : "手机能力只读。");
  }

  function renderDrawerDanger(model) {
    const zone = $("#drawer-danger");
    if (!zone) return;
    clearNode(zone);
    if (model.registrationState === "pending") {
      zone.appendChild(actionButton("批准", "drawer-approve", model.deviceId, "button-primary"));
      zone.appendChild(actionButton("拒绝", "drawer-reject", model.deviceId, "button-danger"));
    } else if (model.registrationState === "approved" && model.kind === "hardware") {
      zone.appendChild(actionButton("撤销", "drawer-revoke", model.deviceId, "button-danger"));
      zone.appendChild(actionButton("删除设备", "drawer-remove-hardware", model.deviceId, "button-danger"));
    } else if (model.registrationState === "approved") {
      zone.appendChild(actionButton("解除手机", "drawer-remove-phone", model.deviceId, "button-danger"));
    } else {
      zone.appendChild(actionButton("重新批准", "drawer-approve", model.deviceId, "button-primary"));
    }
  }

  function applyDevicePayload(payload) {
    const devices = Array.isArray(payload.devices) ? payload.devices : [];
    state.devices = devices.map(mapDeviceToViewModel);
    state.lastUpdatedAt = Date.now();
    renderDeviceTable();
    renderOverview();
    updateStatusBar();
    updatePendingAlert();
  }

  async function loadDevices(options) {
    const settings = options || {};
    try {
      const payload = await api(API.devices);
      applyDevicePayload(payload);
      state.status = state.status || { version: "--" };
      clearError("devices");
      return true;
    } catch (error) {
      recordError("devices", error);
      state.devices = [];
      const body = $("#device-table-body");
      if (body) {
        clearNode(body);
        const cell = build("td");
        cell.colSpan = 5;
        cell.appendChild(emptyFailure(error, "devices"));
        const row = build("tr");
        row.appendChild(cell);
        body.appendChild(row);
      }
      const cards = $("#device-card-list");
      if (cards) {
        clearNode(cards);
        cards.appendChild(emptyFailure(error, "devices"));
      }
      renderOverview();
      updatePendingAlert();
      if (!settings.silent) toast(userMessage(error), "error");
      return false;
    }
  }

  function emptyFailure(error, scope) {
    const box = build("div", "inline-alert");
    box.dataset.state = "error";
    box.appendChild(build("strong", null, userMessage(error) + " "));
    const retry = build("button", "button", "重试");
    retry.type = "button";
    retry.setAttribute("data-action", "retry-all");
    retry.style.marginLeft = "8px";
    box.appendChild(retry);
    return box;
  }

  async function loadStatus() {
    try {
      const payload = await api(API.status);
      state.status = payload;
      clearError("status");
    } catch (error) {
      state.status = null;
      recordError("status", error);
      updateStatusBar();
      return;
    }
    try {
      const codex = await api(API.codexStatus);
      state.codex = codex.codex || null;
      clearError("codex");
    } catch (error) {
      state.codex = null;
      recordError("codex", error);
    }
    renderOverview();
    updateStatusBar();
  }

  function clearError(scope) {
    state.errors = state.errors.filter(function (item) { return item.scope !== scope; });
    if (scope === "status" || scope === "codex") {
      const remaining = state.errors.filter(function (item) { return item.scope === "status"; });
      showGlobalError(remaining.length ? remaining[0].error : null);
    }
    renderOverviewErrors();
  }

  function recordError(scope, error) {
    state.errors = state.errors.filter(function (item) { return item.scope !== scope; });
    state.errors.push({ scope: scope, error: error });
    if (scope === "status") showGlobalError(error);
    updateStatusBar();
    renderOverviewErrors();
  }

  async function registrationAction(deviceId, action, button) {
    const model = deviceById(deviceId);
    const labels = { approve: "批准", reject: "拒绝", revoke: "撤销" };
    if (!model || !labels[action]) return;
    if (action === "reject" || action === "revoke") {
      const confirmed = await confirmAction({
        title: labels[action] + "设备",
        message: action === "revoke"
          ? "撤销“" + model.name + "”后，该设备必须重新注册并获得批准才能继续接入。"
          : "拒绝“" + model.name + "”后，设备需要重新发起注册申请。",
        confirmLabel: labels[action]
      });
      if (!confirmed) return;
    }
    setBusy(button, true, labels[action] + "中...");
    try {
      await api(API[action], { method: "POST", body: { device_id: deviceId } });
      toast("已" + labels[action] + "“" + model.name + "”。", "success");
      await loadDevices({ silent: true });
      if (state.selectedDeviceId === deviceId) {
        const refreshed = deviceById(deviceId);
        if (refreshed) renderDrawer(refreshed);
        else closeDrawer({ force: true, silent: true });
      }
    } catch (error) {
      toast(labels[action] + "失败：" + userMessage(error), "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function removePhone(deviceId, button) {
    const model = deviceById(deviceId);
    if (!model) return;
    const confirmed = await confirmAction({
      title: "解除手机",
      message: "解除“" + model.name + "”后，该手机需要重新配对才能继续使用。",
      confirmLabel: "解除手机"
    });
    if (!confirmed) return;
    setBusy(button, true, "解除中...");
    try {
      await api(API.companion, {
        method: "POST",
        body: { action: "remove_phone", phone_id: deviceId }
      });
      toast("已解除手机“" + model.name + "”。", "success");
      await Promise.all([loadDevices({ silent: true }), loadCompanion()]);
      if (state.selectedDeviceId === deviceId) closeDrawer({ force: true });
    } catch (error) {
      toast("解除失败：" + userMessage(error), "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function removeHardware(deviceId, button) {
    const model = deviceById(deviceId);
    if (!model) return;
    const confirmed = await confirmAction({
      title: "删除设备",
      message: "删除“" + model.name + "”后，CodexBridge 会忘记当前配对记录。设备仍在广播时会重新出现在待批准列表，需要再次批准。",
      confirmLabel: "删除设备"
    });
    if (!confirmed) return;
    setBusy(button, true, "删除中...");
    try {
      await api(API.removeDevice, {
        method: "POST",
        body: { device_id: deviceId }
      });
      toast("已删除设备“" + model.name + "”，等待设备重新发现。", "success");
      await loadDevices({ silent: true });
      if (state.selectedDeviceId === deviceId) {
        const refreshed = deviceById(deviceId);
        if (refreshed) renderDrawer(refreshed);
        else closeDrawer({ force: true, silent: true });
      }
    } catch (error) {
      toast("删除失败：" + userMessage(error), "error");
    } finally {
      setBusy(button, false);
    }
  }

  function drawerBudgetValue() {
    const input = $("#drawer-budget");
    if (!input || input.disabled) return undefined;
    const raw = String(input.value || "").trim();
    if (raw === "") return null;
    const numeric = Number(raw);
    if (!Number.isInteger(numeric) || numeric < 16 || numeric > 100000) return NaN;
    return numeric;
  }

  async function saveDrawerCapabilities(button) {
    const model = deviceById(state.selectedDeviceId);
    if (!model) return;
    const enabled = $$("[data-capability]", $("#drawer-capabilities"))
      .filter(function (input) { return input.checked; })
      .map(function (input) { return input.value; });
    const budget = drawerBudgetValue();
    const budgetError = $("#drawer-budget-error");
    if (Number.isNaN(budget)) {
      if (budgetError) budgetError.textContent = "返回文本量必须是 16 到 100000 之间的整数，或留空表示不限。";
      return;
    }
    if (budgetError) budgetError.textContent = "";
    setBusy(button, true, "保存中...");
    setAlert($("#drawer-message"), "正在保存能力...", "working");
    try {
      const payload = await api(API.capabilities, {
        method: "POST",
        body: {
          device_id: model.deviceId,
          enabled: enabled,
          max_text_chars: budget === undefined ? model.maxTextChars : budget
        }
      });
      model.raw.enabled = payload.enabled || enabled;
      model.enabled = Array.isArray(payload.enabled) ? payload.enabled : enabled;
      if (typeof payload.max_text_chars !== "undefined") {
        model.maxTextChars = payload.max_text_chars;
        model.raw.max_text_chars = payload.max_text_chars;
      }
      const stored = state.devices.find(function (item) { return item.deviceId === model.deviceId; });
      if (stored) {
        stored.enabled = model.enabled;
        stored.maxTextChars = model.maxTextChars;
      }
      renderDeviceTable();
      renderOverview();
      renderDrawer(model);
      state.drawerDirty = false;
      setAlert($("#drawer-message"), "能力设置已保存。", "success");
      toast("能力设置已保存。", "success");
    } catch (error) {
      setAlert($("#drawer-message"), "保存失败：" + userMessage(error), "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function quickSaveCapabilities(deviceId, button) {
    const model = deviceById(deviceId);
    if (!model) return;
    setBusy(button, true, "保存中...");
    try {
      const payload = await api(API.capabilities, {
        method: "POST",
        body: { device_id: deviceId, enabled: model.enabled }
      });
      model.enabled = Array.isArray(payload.enabled) ? payload.enabled : model.enabled;
      toast("能力设置已保存。", "success");
      renderDeviceTable();
    } catch (error) {
      toast("保存失败：" + userMessage(error), "error");
    } finally {
      setBusy(button, false);
    }
  }

  function showPendingFilter() {
    persistDeviceFilter({ status: "pending", kind: "all", query: "", sort: state.deviceFilter.sort });
    syncDeviceFilterControls();
    state.pendingFocus = true;
    setRoute("devices");
    renderDeviceTable();
  }

  function clearDeviceFilter() {
    persistDeviceFilter({ status: "all", kind: "all", query: "", sort: state.deviceFilter.sort });
    syncDeviceFilterControls();
    renderDeviceTable();
  }

  function applyCompanion(payload) {
    const companion = (payload && payload.companion) || {};
    state.companion = companion;
    const toggle = $("#companion-enabled");
    if (toggle) toggle.checked = Boolean(companion.enabled);
    const intermediate = $("#companion-show-intermediate");
    if (intermediate) intermediate.checked = Boolean(companion.show_intermediate_events);
    const addresses = $("#companion-addresses");
    if (addresses) {
      const urls = companion.urls || [];
      addresses.value = urls.length ? urls.join(" · ") : "未检测到局域网地址";
    }
    const companionPort = $("#companion-port");
    if (companionPort) companionPort.value = companion.port ? String(companion.port) : "";
    const discoveryPort = $("#companion-discovery-port");
    if (discoveryPort) discoveryPort.value = companion.discovery_port ? String(companion.discovery_port) : "";
    renderPorts(companion.ports || []);
    applyQueueVisibility();
  }

  function applyQueueVisibility() {
    const nav = $("#queue-nav-item");
    if (nav) nav.hidden = !state.queue.enabled;
    const surface = $("#overview-queue-surface");
    if (surface) surface.hidden = !state.queue.enabled;
    if (!state.queue.enabled && parseHash().route === "queue") {
      setRoute("overview");
    }
  }

  async function loadCompanion() {
    try {
      const payload = await api(API.companion);
      applyCompanion(payload);
      clearError("companion");
      const message = $("#companion-message");
      const companion = state.companion || {};
      setAlert(
        message,
        companion.enabled
          ? "手机连接已开启，已配对手机可直接访问。"
          : "手机连接已关闭，已配对手机需要重新开启后再连接。",
        companion.enabled ? "success" : ""
      );
      return true;
    } catch (error) {
      recordError("companion", error);
      setAlert($("#companion-message"), "读取失败：" + userMessage(error), "error");
      return false;
    }
  }

  async function probeQueue() {
    // Queue support lives behind an admin endpoint owned by the unified device
    // protocol work. Until that endpoint exists the whole queue area stays
    // hidden, so the page never shows a permanently empty shell.
    try {
      const payload = await api(API.queue);
      state.queue.enabled = true;
      state.queue.checked = true;
      state.queue.threads = Array.isArray(payload.threads) ? payload.threads : [];
      applyQueueVisibility();
      renderQueue();
    } catch (_error) {
      state.queue.enabled = false;
      state.queue.checked = true;
      applyQueueVisibility();
    }
  }

  function portInput(kind) {
    if (kind === "discovery") {
      const discovery = $("#companion-discovery-port");
      if (discovery && String(discovery.value || "").trim() !== "") return discovery;
      const rendered = document.querySelector('input[data-port-kind="discovery"]');
      if (rendered) return rendered;
      return discovery;
    }
    const companion = $("#companion-port");
    if (companion && String(companion.value || "").trim() !== "") return companion;
    const rendered = document.querySelector('input[data-port-kind="companion"]');
    return rendered || companion;
  }

  function portStateText(value) {
    return ({
      running: "正常",
      active: "正常",
      error: "异常",
      disabled: "已关闭",
      reserved: "已保留",
      fixed: "设备定义"
    })[value] || value || "未知";
  }

  function renderPorts(ports) {
    const list = $("#companion-port-list");
    if (!list) return;
    clearNode(list);
    if (!ports.length) {
      list.appendChild(build("div", "empty-state", "暂时没有端口状态。"));
      return;
    }
    ports.forEach(function (item) {
      const row = build("div", "port-row");
      const info = build("div");
      info.appendChild(build("strong", null, item.label || item.key));
      info.appendChild(build("p", null, (item.protocol || "") + " · " + (item.purpose || "")));
      row.appendChild(info);
      if (item.editable) {
        const input = build("input", "port-edit");
        input.type = "number";
        input.min = "1";
        input.max = "65535";
        input.inputMode = "numeric";
        input.value = String(item.port || "");
        input.setAttribute("data-port-kind", item.key);
        input.setAttribute("aria-label", (item.label || item.key) + "端口");
        row.appendChild(input);
      } else {
        row.appendChild(build("span", "port-number", String(item.port || "--")));
      }
      const actions = build("div", "port-actions-cell");
      if (item.editable) {
        const save = build("button", "button", "保存");
        save.type = "button";
        save.setAttribute("data-action", "set-port");
        save.setAttribute("data-port-kind", item.key);
        actions.appendChild(save);
      }
      const statusValue = build("span", "port-state", portStateText(item.state));
      statusValue.dataset.state = item.state || "";
      actions.appendChild(statusValue);
      row.appendChild(actions);
      list.appendChild(row);
    });
  }

  async function saveCompanionEnabled() {
    const toggle = $("#companion-enabled");
    if (!toggle) return;
    const desired = toggle.checked;
    toggle.disabled = true;
    setAlert($("#companion-message"), "正在保存手机连接设置...", "working");
    try {
      const payload = await api(API.companion, {
        method: "POST",
        body: { enabled: desired }
      });
      applyCompanion(payload);
      const enabled = state.companion.enabled;
      setAlert(
        $("#companion-message"),
        enabled
          ? "已允许手机连接。"
          : "已关闭手机连接，已配对手机需要重新开启后再连接。",
        "success"
      );
    } catch (error) {
      toggle.checked = state.companion ? Boolean(state.companion.enabled) : !desired;
      setAlert($("#companion-message"), "保存失败：" + userMessage(error), "error");
    } finally {
      toggle.disabled = false;
    }
  }

  async function saveIntermediateEvents() {
    const toggle = $("#companion-show-intermediate");
    if (!toggle) return;
    const desired = toggle.checked;
    toggle.disabled = true;
    setAlert($("#preferences-message"), "正在保存显示设置...", "working");
    try {
      const payload = await api(API.companion, {
        method: "POST",
        body: { show_intermediate_events: desired }
      });
      applyCompanion(payload);
      setAlert(
        $("#preferences-message"),
        desired ? "已开启中间事件，所有已配对设备会看到更多过程信息。" : "已关闭中间事件，仅保留用户消息、最终回答、必要审批和错误通知。",
        "success"
      );
    } catch (error) {
      toggle.checked = state.companion ? Boolean(state.companion.show_intermediate_events) : !desired;
      setAlert($("#preferences-message"), "保存失败：" + userMessage(error), "error");
    } finally {
      toggle.disabled = false;
    }
  }

  async function newCompanionCode(button) {
    const target = button || $("#companion-new-code");
    setBusy(target, true, "生成中...");
    try {
      const payload = await api(API.companion, {
        method: "POST",
        body: { action: "new_code" }
      });
      state.pairing = payload.pairing || null;
      applyCompanion(payload);
      renderPairingCode();
      const message = state.route === "devices" ? null : $("#companion-message");
      if (message) setAlert(message, "请在手机端输入配对码。", "success");
      toast("已生成配对码。", "success");
      if (state.route !== "access") setRoute("access");
    } catch (error) {
      toast("生成失败：" + userMessage(error), "error");
      if (state.route === "access") setAlert($("#companion-message"), "生成失败：" + userMessage(error), "error");
    } finally {
      setBusy(target, false);
    }
  }

  function renderPairingCode() {
    const box = $("#companion-code");
    if (!box) return;
    clearNode(box);
    if (!state.pairing || !state.pairing.code) {
      box.hidden = true;
      return;
    }
    box.appendChild(build("span", null, state.pairing.code));
    const expires = Number(state.pairing.expires_at || 0);
    box.appendChild(build("small", null, expires
      ? "有效期至 " + new Date(expires * 1000).toLocaleTimeString()
      : "输入后使用一次即失效"));
    box.hidden = false;
  }

  async function savePort(kind, input, button) {
    const value = Number(input.value);
    const message = $("#companion-message");
    if (!Number.isInteger(value) || value < 1 || value > 65535) {
      setAlert(message, "端口必须是 1 到 65535 之间的整数。", "error");
      input.focus();
      return;
    }
    if (value === 8790) {
      setAlert(message, "8790 已保留给其他程序，CodexBridge 不能使用。", "error");
      input.focus();
      return;
    }
    setBusy(button, true, "切换中...");
    setAlert(message, "正在验证并切换端口...", "working");
    try {
      const payload = await api(API.companion, {
        method: "POST",
        body: { action: "set_port", kind: kind, port: value }
      });
      applyCompanion(payload);
      const label = kind === "discovery" ? "手机发现端口" : "手机连接端口";
      setAlert(message, label + "已切换为 " + value + "。", "success");
      toast(label + "已更新。", "success");
    } catch (error) {
      setAlert(message, "切换失败：" + userMessage(error) + " 原端口仍保持可用。", "error");
      input.value = String((state.companion && state.companion.port) || "");
    } finally {
      setBusy(button, false);
    }
  }

  function selectedSource() {
    const selected = $('input[name="source"]:checked', $("#speech-form"));
    return selected ? selected.value : "default";
  }

  function syncRemoteFields() {
    const fields = $("#remote-fields");
    if (fields) fields.hidden = selectedSource() !== "openai";
  }

  function speechPayload() {
    return {
      source: selectedSource(),
      endpoint: String(($("#speech-endpoint") || {}).value || "").trim(),
      api_key: String(($("#speech-api-key") || {}).value || "").trim(),
      model: String(($("#speech-model") || {}).value || "").trim() || "whisper-1",
      language: String(($("#speech-language") || {}).value || "").trim() || "zh",
      clear_api_key: Boolean(($("#clear-api-key") || {}).checked)
    };
  }

  function clearSpeechErrors() {
    [
      ["speech-endpoint", "endpoint-error"],
      ["speech-api-key", "key-error"],
      ["speech-model", "model-error"],
      ["speech-language", "language-error"]
    ].forEach(function (pair) {
      const input = document.getElementById(pair[0]);
      if (input) input.removeAttribute("aria-invalid");
      const error = document.getElementById(pair[1]);
      if (error) error.textContent = "";
    });
  }

  function validateSpeech() {
    clearSpeechErrors();
    const payload = speechPayload();
    if (payload.source !== "openai") return true;
    let valid = true;
    try {
      const url = new URL(payload.endpoint);
      if (!["http:", "https:"].includes(url.protocol) || !url.hostname || url.pathname === "/") {
        throw new Error("endpoint");
      }
    } catch (_error) {
      valid = false;
      const input = $("#speech-endpoint");
      if (input) input.setAttribute("aria-invalid", "true");
      setAlert($("#endpoint-error"), "请输入以 http:// 或 https:// 开头、包含接口路径的完整地址。");
    }
    if (!payload.model) {
      valid = false;
      const input = $("#speech-model");
      if (input) input.setAttribute("aria-invalid", "true");
      const error = $("#model-error");
      if (error) error.textContent = "请填写模型名称。";
    }
    return valid;
  }

  function applySpeech(payload) {
    const config = (payload && payload.speech) || {};
    state.speech = config;
    const radio = $('input[name="source"][value="' + (config.source || "default") + '"]');
    if (radio) radio.checked = true;
    const endpoint = $("#speech-endpoint");
    if (endpoint) endpoint.value = config.endpoint || "";
    const model = $("#speech-model");
    if (model) model.value = config.model || "whisper-1";
    const language = $("#speech-language");
    if (language) language.value = config.language || "zh";
    const key = $("#speech-api-key");
    if (key) {
      key.value = "";
      key.placeholder = config.api_key_set ? "已保存，留空表示不修改" : "填写服务提供的 API Key";
    }
    const clear = $("#clear-api-key");
    if (clear) clear.checked = false;
    syncRemoteFields();
  }

  async function loadSpeech() {
    try {
      const payload = await api(API.speech);
      applySpeech(payload);
      setAlert(
        $("#speech-message"),
        state.speech && state.speech.api_key_set ? "已读取语音设置，API Key 已安全保存。" : "已读取语音设置。",
        "success"
      );
    } catch (error) {
      setAlert($("#speech-message"), "读取失败：" + userMessage(error), "error");
    }
  }

  async function saveSpeech(event) {
    if (event) event.preventDefault();
    if (!validateSpeech()) {
      setAlert($("#speech-message"), "请先修正标出的字段。", "error");
      return;
    }
    const button = $("#save-speech");
    setBusy(button, true, "保存中...");
    setAlert($("#speech-message"), "正在保存语音设置...", "working");
    try {
      const payload = await api(API.speech, {
        method: "POST",
        body: speechPayload()
      });
      applySpeech(payload);
      setAlert($("#speech-message"), "语音设置已保存。", "success");
      toast("语音设置已保存。", "success");
    } catch (error) {
      setAlert($("#speech-message"), "保存失败：" + userMessage(error), "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function testSpeech() {
    if (!validateSpeech()) {
      setAlert($("#speech-message"), "请先修正标出的字段。", "error");
      return;
    }
    const button = $("#test-speech");
    setBusy(button, true, "测试中...");
    setAlert($("#speech-message"), "正在请求远程接口，可能需要几秒钟...", "working");
    try {
      const payload = await api(API.speechTest, {
        method: "POST",
        body: speechPayload()
      });
      setAlert($("#speech-message"), payload.message || "连接成功。", "success");
    } catch (error) {
      setAlert($("#speech-message"), "连接失败：" + userMessage(error), "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function loadGeneral() {
    try {
      const payload = await api(API.general);
      state.general = payload.general || {};
      const toggle = $("#startup-enabled");
      if (toggle) toggle.checked = Boolean(state.general.startup_enabled);
      setAlert($("#general-message"), "常规设置已读取。");
      clearError("general");
    } catch (error) {
      recordError("general", error);
      setAlert($("#general-message"), "读取失败：" + userMessage(error), "error");
    }
  }

  async function saveStartup() {
    const toggle = $("#startup-enabled");
    if (!toggle) return;
    const desired = toggle.checked;
    toggle.disabled = true;
    setAlert($("#general-message"), "正在保存开机自启动设置...", "working");
    try {
      const payload = await api(API.general, {
        method: "POST",
        body: { startup_enabled: desired }
      });
      state.general = payload.general || {};
      toggle.checked = Boolean(state.general.startup_enabled);
      setAlert($("#general-message"), toggle.checked ? "已开启开机自启动。" : "已关闭开机自启动。", "success");
    } catch (error) {
      toggle.checked = state.general ? Boolean(state.general.startup_enabled) : !desired;
      setAlert($("#general-message"), "保存失败：" + userMessage(error), "error");
    } finally {
      toggle.disabled = false;
    }
  }

  async function openLogs(button) {
    setBusy(button, true, "打开中...");
    try {
      await api(API.general, { method: "POST", body: { action: "open_logs" } });
      setAlert($("#general-message"), "已打开日志目录。", "success");
    } catch (error) {
      setAlert($("#general-message"), "打开失败：" + userMessage(error), "error");
    } finally {
      setBusy(button, false);
    }
  }

  async function copyAddresses(button) {
    const input = $("#companion-addresses");
    const value = input ? String(input.value || "") : "";
    if (!value || value === "正在读取...") {
      toast("暂时没有可复制的连接地址。", "warning");
      return;
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(value);
      } else if (input) {
        input.select();
        document.execCommand("copy");
      }
      toast("连接地址已复制。", "success");
    } catch (_error) {
      toast("复制失败，请手动选择地址。", "error");
    }
  }

  async function refreshAll(button) {
    if (button) setBusy(button, true, "刷新中...");
    await Promise.all([loadStatus(), loadDevices({ silent: true }), loadCompanion(), loadGeneral(), loadSpeech()]);
    if (button) setBusy(button, false);
    toast("已刷新全部状态。", "success");
  }

  async function loadQueue(options) {
    const settings = options || {};
    if (!state.queue.enabled) return;
    try {
      const payload = await api(API.queue);
      const threads = Array.isArray(payload.threads) ? payload.threads : [];
      state.queue.threads = threads;
      renderQueue();
      clearError("queue");
    } catch (error) {
      if (error.code === "not_found" || error.code === "http_404") {
        state.queue.enabled = false;
        state.queue.checked = true;
        applyQueueVisibility();
        return;
      }
      recordError("queue", error);
      if (!settings.silent) toast(userMessage(error), "error");
    }
  }

  function renderQueue() {
    const list = $("#queue-list");
    if (!list) return;
    clearNode(list);
    const summary = $("#queue-result-summary");
    if (summary) summary.textContent = "共 " + state.queue.threads.length + " 个线程包含待发送项。";
    if (!state.queue.threads.length) {
      list.appendChild(build("div", "empty-state", "当前没有等待发送的线程项。"));
      return;
    }
    state.queue.threads.forEach(function (thread) {
      const block = build("section", "queue-thread");
      const head = build("div", "queue-thread-head");
      const info = build("div");
      info.appendChild(build("strong", null, thread.thread_title || thread.thread_id || "未命名线程"));
      info.appendChild(build("span", null, (thread.items || []).length + " 项等待"));
      head.appendChild(info);
      block.appendChild(head);
      (thread.items || []).forEach(function (item) {
        block.appendChild(queueItem(item, thread));
      });
      list.appendChild(block);
    });
  }

  function queueItem(item, thread) {
    const row = build("article", "queue-item");
    row.appendChild(build("p", "queue-item-summary", item.text || item.summary || "（无内容）"));
    const meta = build("div", "queue-item-meta");
    meta.appendChild(build("span", null, LOGIC.queueStatusLabel(item.status)));
    if (item.created_at) meta.appendChild(build("span", null, absoluteTime(item.created_at)));
    if (item.model) meta.appendChild(build("span", null, item.model));
    if (item.effort) meta.appendChild(build("span", null, item.effort));
    row.appendChild(meta);
    const actions = build("div", "queue-item-actions");
    if (queueCanSteer(item)) {
      actions.appendChild(queueButton(item, thread, "引导", "queue-steer"));
    }
    if (queueCanEdit(item)) {
      actions.appendChild(queueButton(item, thread, "编辑", "queue-edit"));
    }
    if (queueCanDelete(item)) {
      actions.appendChild(queueButton(item, thread, "删除", "queue-delete"));
    }
    if (!actions.childNodes.length) {
      actions.appendChild(build("span", "muted", "发送中，暂不可编辑。"));
    }
    row.appendChild(actions);
    return row;
  }

  function queueButton(item, thread, label, action) {
    const button = build("button", "button", label);
    button.type = "button";
    button.setAttribute("data-action", action);
    button.setAttribute("data-queue-id", item.queue_id || item.id || "");
    button.setAttribute("data-thread-id", thread.thread_id || "");
    button.setAttribute("data-revision", String(item.revision || 0));
    return button;
  }

  function handleAction(event) {
    const target = event.target.closest("[data-action]");
    if (!target) return;
    const action = target.getAttribute("data-action");
    const deviceId = target.getAttribute("data-device-id") || "";
    switch (action) {
      case "new-code":
        event.preventDefault();
        newCompanionCode(target);
        break;
      case "show-pending":
        showPendingFilter();
        break;
      case "clear-filter":
        clearDeviceFilter();
        break;
      case "refresh-devices":
        setBusy(target, true, "刷新中...");
        loadDevices().then(function () { setBusy(target, false); });
        break;
      case "refresh-all":
        refreshAll(target);
        break;
      case "refresh-ports":
        loadCompanion();
        break;
      case "retry-all":
        refreshAll(target);
        break;
      case "open-logs":
        openLogs(target);
        break;
      case "open-drawer":
        openDrawer(deviceId, target);
        break;
      case "approve":
      case "reject":
      case "revoke":
        registrationAction(deviceId, action, target);
        break;
      case "drawer-approve":
        registrationAction(deviceId || state.selectedDeviceId, "approve", target);
        break;
      case "drawer-reject":
        registrationAction(deviceId || state.selectedDeviceId, "reject", target);
        break;
      case "drawer-revoke":
        registrationAction(deviceId || state.selectedDeviceId, "revoke", target);
        break;
      case "remove-phone":
      case "drawer-remove-phone":
        removePhone(deviceId || state.selectedDeviceId, target);
        break;
      case "remove-hardware":
      case "drawer-remove-hardware":
        removeHardware(deviceId || state.selectedDeviceId, target);
        break;
      case "quick-save":
        quickSaveCapabilities(deviceId, target);
        break;
      case "drawer-save":
        saveDrawerCapabilities(target);
        break;
      case "drawer-open-queue": {
        const model = deviceById(state.selectedDeviceId);
        closeDrawer({ force: true, silent: true });
        if (state.queue.enabled && model) {
          state.queueFocus = model.deviceId;
          setRoute("queue");
        }
        break;
      }
      case "queue-steer":
      case "queue-edit":
      case "queue-delete":
        toast("队列操作接口尚未上线。", "warning");
        break;
      case "drawer-close":
        requestDrawerClose();
        break;
      default:
        break;
    }
  }

  function requestDrawerClose() {
    if (state.drawerDirty) {
      confirmAction({
        title: "放弃未保存修改",
        message: "设备详情中有尚未保存的能力或文本预算修改，关闭后会丢失。",
        confirmLabel: "放弃修改"
      }).then(function (confirmed) {
        if (confirmed) closeDrawer();
      });
      return;
    }
    closeDrawer();
  }

  function bindEvents() {
    document.addEventListener("click", handleAction);
    document.addEventListener("click", function (event) {
      const save = event.target.closest('[data-action="set-port"]');
      if (!save) return;
      const kind = save.getAttribute("data-port-kind") || "companion";
      const input = portInput(kind);
      if (input) savePort(kind, input, save);
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Tab" && state.selectedDeviceId && !drawerHidden()) {
        trapDrawerFocus(event);
        return;
      }
      if (event.key === "Escape") {
        if ($("#confirm-dialog") && $("#confirm-dialog").open) {
          settleConfirm(false);
          return;
        }
        if (state.selectedDeviceId) {
          event.preventDefault();
          requestDrawerClose();
          return;
        }
        const search = $("#device-search");
        if (search && document.activeElement === search && search.value) {
          search.value = "";
          const next = Object.assign({}, state.deviceFilter);
          next.query = "";
          persistDeviceFilter(next);
          renderDeviceTable();
        }
        return;
      }
      if (event.key === "/" && !isEditingField(event.target)) {
        const search = $("#device-search");
        if (search && state.route === "devices") {
          event.preventDefault();
          search.focus();
        }
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        if (state.selectedDeviceId) {
          const save = $('[data-action="drawer-save"]');
          if (save && !save.hidden) saveDrawerCapabilities(save);
        }
      }
    });

    const search = $("#device-search");
    let debounce = null;
    if (search) {
      search.addEventListener("input", function () {
        window.clearTimeout(debounce);
        debounce = window.setTimeout(function () {
          const next = Object.assign({}, state.deviceFilter);
          next.query = search.value;
          persistDeviceFilter(next);
          renderDeviceTable();
        }, 200);
      });
    }
    bindSelect("#device-status-filter", "status");
    bindSelect("#device-kind-filter", "kind");
    bindSelect("#device-sort", "sort");

    const enabledToggle = $("#companion-enabled");
    if (enabledToggle) enabledToggle.addEventListener("change", saveCompanionEnabled);
    const intermediate = $("#companion-show-intermediate");
    if (intermediate) intermediate.addEventListener("change", saveIntermediateEvents);
    const startup = $("#startup-enabled");
    if (startup) startup.addEventListener("change", saveStartup);
    const newCode = $("#companion-new-code");
    if (newCode) newCode.addEventListener("click", function () { newCompanionCode(newCode); });
    const copy = $("#copy-addresses");
    if (copy) copy.addEventListener("click", function () { copyAddresses(copy); });
    const queueRefresh = $("#refresh-queue");
    if (queueRefresh) queueRefresh.addEventListener("click", function () { loadQueue(); });

    const speechForm = $("#speech-form");
    if (speechForm) speechForm.addEventListener("submit", saveSpeech);
    const test = $("#test-speech");
    if (test) test.addEventListener("click", testSpeech);
    const toggleKey = $("#toggle-key");
    if (toggleKey) {
      toggleKey.addEventListener("click", function () {
        const input = $("#speech-api-key");
        if (!input) return;
        const visible = input.type === "text";
        input.type = visible ? "password" : "text";
        toggleKey.textContent = visible ? "显示" : "隐藏";
      });
    }
    $$('input[name="source"]').forEach(function (input) {
      input.addEventListener("change", syncRemoteFields);
    });

    const confirmOk = $("#confirm-ok");
    if (confirmOk) confirmOk.addEventListener("click", function () { settleConfirm(true); });
    const confirmCancel = $("#confirm-cancel");
    if (confirmCancel) confirmCancel.addEventListener("click", function () { settleConfirm(false); });
    const dialog = $("#confirm-dialog");
    if (dialog) dialog.addEventListener("cancel", function (event) { event.preventDefault(); settleConfirm(false); });
    const backdrop = $("#drawer-backdrop");
    if (backdrop) backdrop.addEventListener("click", requestDrawerClose);
    const drawerBody = $("#drawer-body");
    if (drawerBody) {
      drawerBody.addEventListener("input", function () { state.drawerDirty = true; });
      drawerBody.addEventListener("change", function () { state.drawerDirty = true; });
    }

    window.addEventListener("hashchange", applyRoute);
  }

  function bindSelect(selector, key) {
    const node = $(selector);
    if (!node) return;
    node.addEventListener("change", function () {
      const next = Object.assign({}, state.deviceFilter);
      next[key] = node.value;
      persistDeviceFilter(next);
      renderDeviceTable();
    });
  }

  function drawerHidden() {
    const drawer = $("#device-drawer");
    return !drawer || drawer.hidden;
  }

  function trapDrawerFocus(event) {
    const drawer = $("#device-drawer");
    if (!drawer) return;
    const focusable = $$(
      'button:not([hidden]):not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      drawer,
    ).filter(function (node) { return node.offsetParent !== null; });
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function isEditingField(target) {
    if (!target) return false;
    const tag = String(target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || target.isContentEditable;
  }

  async function bootstrap() {
    bindEvents();
    syncDeviceFilterControls();
    applyRoute();
    await Promise.all([loadStatus(), loadDevices(), loadCompanion(), probeQueue()]);
    loadGeneral();
    loadSpeech();
    updateStatusBar();
    updatePendingAlert();
    renderOverview();
    applyRoute();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }
})();
window.__CB_APP_LOADED__ = true;
"""
