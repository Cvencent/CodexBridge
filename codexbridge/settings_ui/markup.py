from __future__ import annotations

from codexbridge.settings_ui.icons import ICON_SPRITE


MARKUP = r"""
<a class="skip-link" href="#main-content">跳到主要内容</a>
""" + ICON_SPRITE + r"""
<header class="topbar">
  <div class="topbar-inner">
    <div class="brand">
      <span class="brand-mark"><svg class="icon icon-lg" aria-hidden="true"><use href="#icon-monitor"></use></svg></span>
      <span class="brand-copy"><strong>CodexBridge</strong><span>管理中心</span></span>
    </div>
    <div class="topbar-status">
      <div id="bridge-status" class="service-status" role="status" aria-live="polite">正在连接本机服务</div>
      <button id="pending-alert" class="pending-alert" type="button" data-action="show-pending" hidden>
        <svg class="icon icon-sm" aria-hidden="true"><use href="#icon-alert"></use></svg>
        <span id="pending-alert-count">0 台设备待批准</span>
      </button>
    </div>
  </div>
</header>

<div class="app-shell">
  <nav class="side-nav" aria-label="管理中心分区">
    <a id="overview-nav-item" class="route-link" href="#overview" data-route-link="overview">
      <svg class="icon" aria-hidden="true"><use href="#icon-monitor"></use></svg><span>概览</span>
    </a>
    <a id="devices-nav-item" class="route-link" href="#devices" data-route-link="devices">
      <svg class="icon" aria-hidden="true"><use href="#icon-devices"></use></svg><span>设备</span><span id="devices-nav-count" class="nav-count" hidden>0</span>
    </a>
    <a id="queue-nav-item" class="route-link" href="#queue" data-route-link="queue" hidden>
      <svg class="icon" aria-hidden="true"><use href="#icon-queue"></use></svg><span>队列</span><span id="queue-nav-count" class="nav-count" hidden>0</span>
    </a>
    <a id="access-nav-item" class="route-link" href="#access" data-route-link="access">
      <svg class="icon" aria-hidden="true"><use href="#icon-plug"></use></svg><span>接入</span>
    </a>
    <a id="speech-nav-item" class="route-link" href="#speech" data-route-link="speech">
      <svg class="icon" aria-hidden="true"><use href="#icon-mic"></use></svg><span>语音</span>
    </a>
    <a id="preferences-nav-item" class="route-link" href="#preferences" data-route-link="preferences">
      <svg class="icon" aria-hidden="true"><use href="#icon-sliders"></use></svg><span>偏好</span>
    </a>
  </nav>

  <main id="main-content" class="main-content">
    <section id="overview-panel" class="page-panel" data-route-panel="overview" aria-labelledby="overview-title">
      <div class="panel-heading">
        <div><h1 id="overview-title">概览</h1><p>先判断服务、待批准设备和 Codex 状态，再进入对应区域处理。</p></div>
        <div class="panel-heading-actions">
          <button class="button" type="button" data-route-go="devices"><svg class="icon" aria-hidden="true"><use href="#icon-devices"></use></svg>查看设备</button>
          <button id="overview-new-code" class="button button-primary" type="button" data-action="new-code"><svg class="icon" aria-hidden="true"><use href="#icon-plug"></use></svg>生成手机配对码</button>
        </div>
      </div>
      <div id="global-error" class="inline-alert" data-state="error" hidden>
        <strong>本机服务连接异常。</strong><span id="global-error-message"></span>
        <button class="button button-ghost" type="button" data-action="retry-all">重试</button>
      </div>
      <div class="overview-grid">
        <div>
          <section class="surface" aria-labelledby="overview-service-title">
            <div class="surface-header"><div><h2 id="overview-service-title">服务与 Codex</h2><p>本机服务、版本、设备在线数和当前 Codex 状态。</p></div></div>
            <div class="surface-body"><div class="metrics">
              <div class="metric"><span class="metric-label">本机服务</span><strong id="overview-service" class="metric-value">读取中</strong><p id="overview-version" class="metric-detail">版本 --</p></div>
              <div class="metric"><span class="metric-label">在线设备</span><strong id="overview-online" class="metric-value">--</strong><p id="overview-online-detail" class="metric-detail">共 -- 台</p></div>
              <div class="metric"><span class="metric-label">待批准</span><strong id="overview-pending" class="metric-value">--</strong><p class="metric-detail">需要本机决策</p></div>
              <div class="metric"><span class="metric-label">Codex</span><strong id="overview-codex" class="metric-value">读取中</strong><p id="overview-codex-detail" class="metric-detail">--</p></div>
            </div></div>
          </section>
          <section class="surface" aria-labelledby="overview-pending-title">
            <div class="surface-header"><div><h2 id="overview-pending-title">待批准设备</h2><p>批准或拒绝会立即更新设备中心。</p></div><button class="button button-ghost" type="button" data-route-go="devices">查看全部</button></div>
            <div id="overview-pending-list" class="surface-body tight"><div class="empty-state">正在读取...</div></div>
          </section>
          <section id="overview-queue-surface" class="surface" aria-labelledby="overview-queue-title" hidden>
            <div class="surface-header"><div><h2 id="overview-queue-title">线程队列</h2><p>队列属于线程，不属于单台设备。</p></div><button class="button button-ghost" type="button" data-route-go="queue">打开队列</button></div>
            <div id="overview-queue-summary" class="surface-body tight"><div class="empty-state">当前没有待发送项。</div></div>
          </section>
        </div>
        <div>
          <section class="surface" aria-labelledby="overview-device-title">
            <div class="surface-header"><div><h2 id="overview-device-title">设备摘要</h2><p>注册状态与在线状态分开统计。</p></div></div>
            <div class="surface-body tight"><div class="summary-list">
              <div class="summary-row"><span>已批准</span><strong id="summary-approved">--</strong></div>
              <div class="summary-row"><span>在线</span><strong id="summary-online">--</strong></div>
              <div class="summary-row"><span>离线</span><strong id="summary-offline">--</strong></div>
              <div class="summary-row"><span>已拒绝 / 已停用</span><strong id="summary-blocked">--</strong></div>
            </div></div>
          </section>
          <section class="surface" aria-labelledby="overview-error-title">
            <div class="surface-header"><div><h2 id="overview-error-title">需要处理</h2><p>接口失败时会在这里保留恢复入口。</p></div></div>
            <div id="overview-error-list" class="surface-body tight"><div class="empty-state">当前没有错误。</div></div>
          </section>
        </div>
      </div>
    </section>

    <section id="devices-panel" class="page-panel" data-route-panel="devices" aria-labelledby="devices-title" hidden>
      <div class="panel-heading">
        <div><h1 id="devices-title">设备</h1><p>手机和硬件共用一份设备记录；待批准设备固定优先显示。</p></div>
        <div class="panel-heading-actions"><button id="refresh-devices" class="button" type="button" data-action="refresh-devices"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>刷新设备</button></div>
      </div>
      <section class="surface" aria-labelledby="device-list-title">
        <div class="surface-header"><div><h2 id="device-list-title">设备中心</h2><p id="device-result-summary">正在读取设备...</p></div></div>
        <div class="toolbar" aria-label="设备筛选">
          <div class="search-field">
            <svg class="icon icon-sm" aria-hidden="true"><use href="#icon-search"></use></svg>
            <label class="sr-only" for="device-search">搜索设备</label>
            <input id="device-search" type="search" placeholder="搜索名称、型号、ID 或地址" autocomplete="off">
          </div>
          <div class="select-field"><label for="device-status-filter">状态</label><select id="device-status-filter">
            <option value="all">全部</option><option value="pending">待批准</option><option value="approved">已批准</option><option value="rejected">已拒绝</option><option value="revoked">已停用</option>
          </select></div>
          <div class="select-field"><label for="device-kind-filter">类型</label><select id="device-kind-filter">
            <option value="all">全部</option><option value="phone">手机</option><option value="hardware">硬件</option>
          </select></div>
          <div class="select-field"><label for="device-sort">排序</label><select id="device-sort">
            <option value="default">默认排序</option><option value="name">名称</option><option value="last_seen">最近在线</option><option value="added">添加时间</option>
          </select></div>
        </div>
        <div id="device-table-wrap" class="device-table-wrap">
          <table class="device-table">
            <thead><tr>
              <th class="col-state" data-column="state">状态</th>
              <th data-column="device">设备</th>
              <th class="col-capabilities" data-column="capabilities">能力</th>
              <th class="col-last-seen" data-column="last-seen">最近连接</th>
              <th class="col-actions" data-column="actions">操作</th>
            </tr></thead>
            <tbody id="device-table-body"><tr><td colspan="5"><div class="empty-state">正在读取设备...</div></td></tr></tbody>
          </table>
        </div>
        <div id="device-card-list" class="device-cards" aria-live="polite"><div class="empty-state">正在读取设备...</div></div>
      </section>
    </section>

    <section id="queue-panel" class="page-panel" data-route-panel="queue" aria-labelledby="queue-title" hidden>
      <div class="panel-heading">
        <div><h1 id="queue-title">队列</h1><p>队列按线程组织，设备行只提供上下文入口。</p></div>
        <div class="panel-heading-actions"><button id="refresh-queue" class="button" type="button" data-action="refresh-queue"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>刷新队列</button></div>
      </div>
      <section class="surface" aria-labelledby="queue-list-title">
        <div class="surface-header"><div><h2 id="queue-list-title">线程队列</h2><p id="queue-result-summary">正在检查队列能力...</p></div></div>
        <div id="queue-list" class="surface-body" aria-live="polite"><div class="empty-state">正在读取队列...</div></div>
      </section>
    </section>

    <section id="access-panel" class="page-panel" data-route-panel="access" aria-labelledby="access-title" hidden>
      <div class="panel-heading"><div><h1 id="access-title">接入</h1><p>这里只管理手机如何找到并连接本机。</p></div></div>
      <section class="surface" aria-labelledby="access-phone-title">
        <div class="surface-header"><div><h2 id="access-phone-title">手机连接</h2><p>已配对手机通过局域网伴侣接口访问，不经过云端。</p></div><input id="companion-enabled" class="switch" type="checkbox" role="switch" aria-label="允许手机连接"></div>
        <div class="surface-body">
          <div id="companion-message" class="inline-alert" role="status" aria-live="polite">正在读取手机接入设置...</div>
          <div class="section-group">
            <h3 class="section-label">配对</h3>
            <p class="field-hint">在手机端输入，10 分钟内有效，使用一次后立即失效。</p>
            <div class="button-row" style="margin-top:8px"><button id="companion-new-code" class="button button-primary" type="button" data-action="new-code"><svg class="icon" aria-hidden="true"><use href="#icon-plug"></use></svg>生成配对码</button></div>
            <div id="companion-code" class="pair-code" hidden></div>
          </div>
          <div class="section-group">
            <h3 class="section-label">端口</h3>
            <p class="field-hint">手机服务端口和手机发现端口都可修改；修改时会先验证新端口，失败时保留原端口。8790 已保留，CodexBridge 不会使用。</p>
            <div class="field" style="margin-top:10px"><label for="companion-port">手机服务端口</label><input id="companion-port" type="number" min="1" max="65535" step="1" inputmode="numeric" aria-describedby="companion-port-hint"><p id="companion-port-hint" class="field-hint">手机通过该 TCP 端口访问 CodexBridge，范围 1-65535，不能使用 8790。</p></div>
            <div class="field" style="margin-top:10px"><label for="companion-discovery-port">手机发现端口</label><input id="companion-discovery-port" type="number" min="1" max="65535" step="1" inputmode="numeric" aria-describedby="companion-discovery-port-hint"><p id="companion-discovery-port-hint" class="field-hint">手机通过该 UDP 端口扫描电脑，范围 1-65535，不能使用 8790。</p></div>
            <div class="form-actions"><button id="companion-save-port" class="button button-primary" type="button" data-action="set-port" data-port-kind="companion"><svg class="icon" aria-hidden="true"><use href="#icon-check"></use></svg>保存手机服务端口</button><button id="companion-save-discovery-port" class="button" type="button" data-action="set-port" data-port-kind="discovery">保存手机发现端口</button></div>
            <div id="companion-port-list" class="port-list" style="margin-top:10px" aria-live="polite"><div class="empty-state">正在读取...</div></div>
            <div class="form-actions"><button id="companion-refresh-ports" class="button" type="button" data-action="refresh-ports"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>刷新端口状态</button></div>
          </div>
        </div>
      </section>
    </section>

    <section id="speech-panel" class="page-panel" data-route-panel="speech" aria-labelledby="speech-title" hidden>
      <div class="panel-heading"><div><h1 id="speech-title">语音</h1><p>Dotii 和 AI Passport 共用这一套语音识别配置。</p></div></div>
      <section class="surface" aria-labelledby="speech-config-title">
        <div class="surface-header"><div><h2 id="speech-config-title">语音识别</h2><p>远程识别失败时自动回退内置识别。</p></div></div>
        <div class="surface-body"><form id="speech-form" novalidate>
          <fieldset class="source-options"><legend class="section-label">识别方式</legend>
            <label class="source-option"><input type="radio" name="source" value="default" checked><span><strong>内置识别</strong><span>优先使用本机离线模型，失败时回退 Windows 语音识别。</span></span></label>
            <label class="source-option"><input type="radio" name="source" value="openai"><span><strong>OpenAI 兼容接口</strong><span>使用兼容 /v1/audio/transcriptions 的远程服务。</span></span></label>
          </fieldset>
          <div id="remote-fields" class="form-grid" hidden>
            <input type="text" autocomplete="username" tabindex="-1" aria-hidden="true" style="position:fixed;left:-10000px;top:auto;width:1px;height:1px;overflow:hidden">
            <div class="field span-2"><label for="speech-endpoint">接口地址</label><input id="speech-endpoint" name="endpoint" type="url" inputmode="url" autocomplete="url" placeholder="https://example.com/v1/audio/transcriptions" aria-describedby="endpoint-hint endpoint-error"><p id="endpoint-hint" class="field-hint">填写完整的转写接口地址，必须以 http:// 或 https:// 开头。</p><p id="endpoint-error" class="field-error" aria-live="polite"></p></div>
            <div class="field span-2"><label for="speech-api-key">API Key</label><div class="key-row"><input id="speech-api-key" name="api_key" type="password" autocomplete="new-password" aria-describedby="key-hint key-error"><button id="toggle-key" class="button" type="button" aria-controls="speech-api-key">显示</button></div><p id="key-hint" class="field-hint">已保存的 Key 不会回传到网页；留空表示继续使用原来的 Key。</p><p id="key-error" class="field-error" aria-live="polite"></p></div>
            <div class="field"><label for="speech-model">模型</label><input id="speech-model" name="model" type="text" value="whisper-1" autocomplete="off" aria-describedby="model-error"><p id="model-error" class="field-error" aria-live="polite"></p></div>
            <div class="field"><label for="speech-language">语言</label><input id="speech-language" name="language" type="text" value="zh" autocomplete="off" aria-describedby="language-error"><p id="language-error" class="field-error" aria-live="polite"></p></div>
            <label class="check-row span-2" for="clear-api-key"><input id="clear-api-key" name="clear_api_key" type="checkbox"><span>清除已保存的 API Key</span></label>
          </div>
          <div class="form-actions"><button id="save-speech" class="button button-primary" type="submit">保存语音设置</button><button id="test-speech" class="button" type="button">测试连接</button></div>
          <div id="speech-message" class="inline-alert" role="status" aria-live="polite">正在读取语音设置...</div>
        </form><p class="inline-alert">远程接口失败时会自动回退内置识别，避免设备完全无法使用语音输入。</p></div>
      </section>
    </section>

    <section id="preferences-panel" class="page-panel" data-route-panel="preferences" aria-labelledby="preferences-title" hidden>
      <div class="panel-heading"><div><h1 id="preferences-title">偏好</h1><p>全局显示、启动和运行维护设置。</p></div></div>
      <section class="surface" aria-labelledby="preferences-display-title">
        <div class="surface-header"><div><h2 id="preferences-display-title">显示与通知</h2><p>该设置影响所有已配对设备，不是单台设备能力。</p></div></div>
        <div class="surface-body"><div class="check-row" style="justify-content:space-between"><span><strong>显示中间事件</strong><span class="field-hint">开启后会向已配对设备发送 commentary、reasoning、命令输出、文件变更、工具结果和上下文提示；关闭时只保留用户消息、最终回答、必要审批和错误通知。</span></span><input id="companion-show-intermediate" class="switch" type="checkbox" role="switch" aria-label="显示中间事件"></div><div id="preferences-message" class="inline-alert" role="status" aria-live="polite"></div></div>
      </section>
      <section class="surface" aria-labelledby="preferences-startup-title">
        <div class="surface-header"><div><h2 id="preferences-startup-title">启动与运行</h2><p>控制开机自启动和本页状态刷新。</p></div></div>
        <div class="surface-body">
          <div class="check-row" style="justify-content:space-between"><span><strong>开机自启动</strong><span class="field-hint">登录 Windows 后自动运行 CodexBridge。</span></span><input id="startup-enabled" class="switch" type="checkbox" role="switch" aria-label="开机自启动"></div>
          <div class="form-actions"><button id="refresh-general" class="button" type="button" data-action="refresh-all"><svg class="icon" aria-hidden="true"><use href="#icon-refresh"></use></svg>刷新全部状态</button></div>
          <div id="general-message" class="inline-alert" role="status" aria-live="polite">正在读取常规设置...</div>
        </div>
      </section>
      <section class="surface" aria-labelledby="preferences-maintenance-title">
        <div class="surface-header"><div><h2 id="preferences-maintenance-title">运行维护</h2><p>连接地址仅供本机复制；日志目录用于故障排查。</p></div></div>
        <div class="surface-body"><div class="field"><label>连接地址</label><div class="key-row"><input id="companion-addresses" type="text" readonly value="正在读取..."><button id="copy-addresses" class="button" type="button"><svg class="icon" aria-hidden="true"><use href="#icon-copy"></use></svg>复制</button></div></div><div class="form-actions"><button id="open-logs" class="button" type="button" data-action="open-logs"><svg class="icon" aria-hidden="true"><use href="#icon-external"></use></svg>打开日志目录</button></div></div>
      </section>
    </section>
  </main>
</div>

<div id="drawer-backdrop" class="drawer-backdrop" hidden></div>
<aside id="device-drawer" class="device-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title" hidden>
  <div class="drawer-header"><div><h2 id="drawer-title">设备详情</h2><p id="drawer-subtitle"></p></div><button class="button button-icon button-ghost" type="button" data-action="drawer-close" aria-label="关闭设备详情"><svg class="icon" aria-hidden="true"><use href="#icon-x"></use></svg></button></div>
  <div class="drawer-body">
    <section class="drawer-section"><div id="drawer-state" class="state-stack"></div></section>
    <section class="drawer-section"><h3>身份</h3><dl id="drawer-identity" class="drawer-facts"></dl></section>
    <section class="drawer-section"><h3>连接与凭据</h3><dl id="drawer-connection" class="drawer-facts"></dl></section>
    <section class="drawer-section"><h3>能力与文本</h3><div id="drawer-capabilities" class="capability-grid"></div><div class="field" style="margin-top:14px"><label for="drawer-budget">返回文本量</label><input id="drawer-budget" type="number" min="16" max="100000" step="1" inputmode="numeric"><p class="field-hint">留空表示不限，并按设备声明；填写值会覆盖设备声明。</p><p id="drawer-budget-error" class="field-error" aria-live="polite"></p></div><div id="drawer-message" class="inline-alert" role="status" aria-live="polite"></div></section>
    <section id="drawer-queue-section" class="drawer-section" hidden><h3>运行</h3><div class="summary-row"><span>活动线程队列</span><strong id="drawer-queue-count">0 项</strong></div><div class="form-actions"><button class="button" type="button" data-action="drawer-open-queue">查看线程队列</button></div></section>
    <section class="drawer-section"><h3>危险区</h3><div id="drawer-danger" class="button-row"></div></section>
  </div>
  <div class="drawer-footer"><div class="button-row"><button class="button button-primary" type="button" data-action="drawer-save">保存能力</button><button class="button" type="button" data-action="drawer-close">关闭</button><button class="button button-danger" type="button" data-action="drawer-reject" hidden>拒绝</button><button class="button button-danger" type="button" data-action="drawer-revoke" hidden>撤销</button><button class="button button-danger" type="button" data-action="drawer-remove-phone" hidden>解除手机</button></div></div>
</aside>

<dialog id="confirm-dialog" class="confirm-dialog" aria-labelledby="confirm-title" aria-describedby="confirm-message">
  <div class="dialog-body"><h2 id="confirm-title">确认操作</h2><p id="confirm-message"></p></div>
  <div class="dialog-actions"><button id="confirm-cancel" class="button" type="button">取消</button><button id="confirm-ok" class="button button-danger" type="button">确认</button></div>
</dialog>

<div id="toast-region" class="toast-region" role="status" aria-live="polite"></div>
"""
