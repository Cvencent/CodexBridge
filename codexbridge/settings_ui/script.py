from __future__ import annotations


LOGIC_JS = r'''
(function (root) {
  "use strict";

  const FEATURES = {
    status: "状态",
    text: "文字消息",
    voice: "语音指令",
    approval: "审批",
    tasks: "任务",
    audio: "录音",
    read_ack: "已读同步",
    results: "任务结果",
    word_bear: "词熊",
    quota: "配额",
    control: "控制",
    queue: "待发送队列"
  };
  const REGISTRATION_LABELS = {
    pending: "待批准",
    approved: "已批准",
    rejected: "已拒绝",
    revoked: "已停用"
  };
  const QUEUE_STATUS_LABELS = {
    pending: "等待发送",
    dispatching: "正在发送",
    failed: "发送失败"
  };
  const DEVICE_FILTER_STATUSES = ["all", "pending", "approved", "rejected", "revoked"];
  const DEVICE_FILTER_KINDS = ["all", "phone", "hardware"];
  const DEVICE_FILTER_SORTS = ["default", "name", "last_seen", "added"];

  function arrayOfStrings(value) {
    if (!Array.isArray(value)) return [];
    return Array.from(new Set(value.filter((item) => typeof item === "string" && item)));
  }

  function finiteOptionalText() {
    for (let index = 0; index < arguments.length; index += 1) {
      const candidate = arguments[index];
      if (typeof candidate === "number") return candidate;
    }
    return null;
  }

  function finiteNumber(value) {
    const number = Number(value);
    return Number.isFinite(number) ? number : 0;
  }

  function mapDeviceToViewModel(device) {
    const source = device && typeof device === "object" ? device : {};
    const inferredPhone = source.kind === "phone" || source.transport === "companion_http" || Boolean(source.phone_id);
    const supported = arrayOfStrings(source.supported || source.capabilities);
    const enabled = arrayOfStrings(source.enabled || source.enabled_capabilities);
    const registrationState = String(source.registration_state || source.registrationState || source.state || "approved");
    return {
      raw: source,
      deviceId: String(source.device_id || source.deviceId || source.phone_id || source.id || ""),
      kind: inferredPhone ? "phone" : "hardware",
      transport: String(source.transport || (inferredPhone ? "companion_http" : "device_tcp")),
      name: String(source.name || source.device_name || source.model || source.device_id || source.id || "未命名设备"),
      model: String(source.model || source.device || "未知型号"),
      registrationState,
      registrationMethod: String(source.registration_method || (inferredPhone ? "pairing_code" : "admin_approval")),
      online: registrationState === "approved" && Boolean(source.online),
      lastSeen: finiteNumber(source.last_seen || source.lastSeen || source.last_seen_at || source.seen_at),
      lastAddress: String(source.last_address || source.lastAddress || source.address || source.host || ""),
      supported,
      enabled,
      maxTextChars: finiteOptionalText(source.max_text_chars, source.maxTextChars),
      hasToken: Boolean(source.has_token || source.hasToken || source.token || inferredPhone),
      queuedCount: Math.max(0, finiteNumber(source.queued_count === undefined ? source.queuedCount : source.queued_count)),
      addedAt: finiteNumber(source.created_at || source.added_at || source.addedAt || source.first_seen || source.last_seen || source.lastSeen),
      firmware: String(source.firmware || source.client_version || ""),
      protocolVersion: String(source.protocol_version || source.client_version || "")
    };
  }

  function statusRank(device) {
    const model = mapDeviceToViewModel(device);
    if (model.registrationState === "pending") return 0;
    if (model.registrationState === "approved" && model.online) return 1;
    if (model.registrationState === "approved" && !model.online) return 2;
    return 3;
  }

  function sortDevices(devices) {
    return (Array.isArray(devices) ? devices.slice() : []).sort((left, right) => {
      const a = mapDeviceToViewModel(left);
      const b = mapDeviceToViewModel(right);
      const rankA = statusRank(a);
      const rankB = statusRank(b);
      if (rankA !== rankB) return rankA - rankB;
      // Pending devices queue oldest-application-first; every other bucket
      // lists the most recent activity first.
      if (rankA === 0) return a.lastSeen - b.lastSeen;
      return b.lastSeen - a.lastSeen;
    });
  }

  function filterDevices(devices, filter) {
    const current = Object.assign({status: "all", kind: "all", query: ""}, filter || {});
    const query = String(current.query || "").trim().toLocaleLowerCase();
    return (Array.isArray(devices) ? devices : []).filter((device) => {
      const model = mapDeviceToViewModel(device);
      if (current.status !== "all" && model.registrationState !== current.status) return false;
      if (current.kind !== "all" && model.kind !== current.kind) return false;
      if (!query) return true;
      return [model.name, model.model, model.deviceId, model.lastAddress].join(" ").toLocaleLowerCase().includes(query);
    });
  }

  function normalizeDeviceFilter(value) {
    const source = value && typeof value === "object" ? value : {};
    return {
      status: DEVICE_FILTER_STATUSES.includes(source.status) ? source.status : "all",
      kind: DEVICE_FILTER_KINDS.includes(source.kind) ? source.kind : "all",
      query: typeof source.query === "string" ? source.query.slice(0, 300) : "",
      sort: DEVICE_FILTER_SORTS.includes(source.sort) ? source.sort : "default"
    };
  }

  function deviceCredentialLabel(device) {
    const model = mapDeviceToViewModel(device);
    if (model.registrationState === "pending") return "等待批准";
    return model.hasToken ? "已登记" : "缺少令牌";
  }

  function formatRelativeTime(now, timestamp) {
    const value = finiteNumber(timestamp);
    if (!value) return "从未";
    const seconds = Math.max(0, finiteNumber(now) - value);
    if (seconds < 60) return "刚刚";
    if (seconds < 3600) return Math.floor(seconds / 60) + " 分钟前";
    if (seconds < 86400) return Math.floor(seconds / 3600) + " 小时前";
    const date = new Date(value * 1000);
    return (date.getMonth() + 1) + "月" + date.getDate() + "日";
  }

  function formatAbsoluteTime(timestamp) {
    const value = finiteNumber(timestamp);
    if (!value) return "从未连接";
    const date = new Date(value * 1000);
    return Number.isNaN(date.getTime()) ? "从未连接" : date.toLocaleString();
  }

  function queueCanEdit(item) { return Boolean(item && ["pending", "failed"].includes(item.status)); }
  function queueCanSteer(item) { return Boolean(item && item.status === "pending"); }
  function queueCanDelete(item) { return Boolean(item && ["pending", "failed"].includes(item.status)); }

  root.__CB_LOGIC__ = {
    FEATURES,
    REGISTRATION_LABELS,
    QUEUE_STATUS_LABELS,
    mapDeviceToViewModel,
    sortDevices,
    filterDevices,
    normalizeDeviceFilter,
    deviceCredentialLabel,
    formatRelativeTime,
    formatAbsoluteTime,
    queueCanEdit,
    queueCanSteer,
    queueCanDelete,
    featureName(value) { return FEATURES[value] || value; },
    registrationLabel(value) { return REGISTRATION_LABELS[value] || value || "未知"; },
    queueStatusLabel(value) { return QUEUE_STATUS_LABELS[value] || value || "未知"; }
  };
})(typeof window === "undefined" ? globalThis : window);
'''

from codexbridge.settings_ui.app_script import APP_JS

__all__ = ["LOGIC_JS", "APP_JS"]
