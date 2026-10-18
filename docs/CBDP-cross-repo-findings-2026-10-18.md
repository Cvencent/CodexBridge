# CBDP 跨仓库联调问题清单（2026-10-18）

本文件由 CodexBridge 侧整理，只记录**不属于本仓库**、需要手机端或设备端处理的
问题。Bridge 自己的协议错误已在 `CodexBridge v2.1.0` 修复，权威定义见
`docs/CBDP-V1-PROTOCOL.md` 19.6 节。

## A. 给 ESP / AI Passport 固件端（`one2passport-publish`）

### A1. 音频事务的 `tx_id` 从不递增（必须修）

`firmware/main/coding_bridge.c` 里 `s_wireless_tx_id` 只在文本路径递增：

```c
/* send_text_bytes() */
s_wireless_tx_id++;
if (s_wireless_tx_id == 0U) s_wireless_tx_id = 1U;
```

而 `send_audio_begin()` / `send_audio_chunk()` / `send_audio_end()` 都直接使用
当前的 `s_wireless_tx_id`，没有递增，`make_idem(s_audio_idem, s_wireless_tx_id)`
也对每段录音生成同一个 `idem`。后果：

1. 连续两次录音复用同一个 `tx_id` 和 `idem`，与“每次事务使用新 `tx_id`”
   的协议意图不符；
2. 只要上一段音频没有正常走到 `AUDIO_END`（见 A2），Bridge 侧的 `tx_id`
   事务仍在，下一次 `AUDIO_BEGIN` 会被判为 `IDEMPOTENCY_CONFLICT`，且之后
   每段录音都会持续失败，必须重启连接才能恢复。

建议：每次 `AUDIO_BEGIN` 前都推进 `tx_id`（文本路径已是这个模式），并让
`idem` 随 `tx_id` 变化；失败重试要么复用同一 `tx_id`+`idem`，要么明确开新事务。
同时建议确认 `wireless_bridge_send_frame()` 的语义是“全部写入”还是“部分写入”，
如果可能部分写入，只按真实写入量推进 `offset`。

### A2. 超过 `max_audio_bytes` 时没有结束事务（建议修）

同样在录音循环里，超过上限时直接：

```c
s_capture_requested = false;
capture_ok = false;
send_capture_error("录音超过电脑端允许的长度");
break;
```

设备没有再发 `AUDIO_END`。Bridge 侧的事务会一直留在 `_transactions` 里，直到
连接断开才清理。协议 19.4 允许 Bridge 提前拒绝，但设备侧更干净的做法是
主动发 `AUDIO_END`（或至少在同一 `tx_id` 上结束），让 Bridge 能立即释放事务。

### A3. 确认 `AUDIO_BEGIN` 是否总是携带线程选择器（需确认）

固件现在发送 `s_active_thread_ref` / `s_active_thread_id`，都没有时才置
`use_active_thread=1`，这是正确的。请确认在“尚未选中任何会话”时确实走
`use_active_thread=1`，否则 Bridge 会按协议返回
`ERROR(INVALID_STATE, NO_ACTIVE_THREAD)`，设备需要能展示这个错误而不是停在
“正在发送到电脑端”。

### A4. 固件的 TLV 编号已正确（无需修改）

核对结果：`codex_bridge_protocol.h` 中 `CODEC=0x0073`、`SAMPLE_RATE=0x0074`、
`CHANNELS=0x0075`、`BITS_PER_SAMPLE=0x0076`、`DURATION_MS=0x0077`、
`TRANSCRIPT=0x0078`、`SHA256=0x0079`、`AUDIO_FORMAT=0x007A`、
`TOTAL_BYTES=0x0047`、`OFFSET=0x0048` 与协议一致。这次编号错位在 Bridge 侧，
已由 v2.1.0 修复。

## B. 给手机端（`CodexBridgeMobile`）

### B1. 已按协议实现的部分（无需修改）

- `CbdpTags.kt` 的音频 tag（`0x0073..0x007A`）、`OBJECT=0x00A0`、
  `OBJECTS=0x00A1`、`NEXT_CURSOR=0x00A5`、`MODEL_CAPABILITIES=0x019E` 均与
  协议一致。
- `CbdpMapper.decodeArrayElements()` 已经按 `u16 count` 解析数组，方向正确。
  之前列表为空是 Bridge 少写计数前缀，v2.1.0 已修。

### B2. 分页需要把 `next_cursor` 回传（需确认）

Bridge v2.1.0 起会对上游不透明字符串游标做会话内 u64 映射，客户端只需把
`RESULT.next_cursor` 原样填回下一次 `threads.list` / `thread.read` 的
`cursor` 参数即可。请确认手机端在 `next_cursor > 0` 时会继续请求下一页，
而不是只取第一页。

### B3. `thread.read` 顶层字段已补齐（无需修改）

v2.1.0 的 `thread.read` RESULT 现在同时携带顶层 `thread_ref`、`thread_id`、
`status`，以及 `objects` 里的 thread/message/approval 对象。手机端可以继续
按现有 mapper 读取。

### B4. 审批对象现在带完整必需字段（无需修改）

`approvals.list` 与 `thread.read` 里的 `approval` 对象现在携带
`approval_ref / approval_id / thread_ref / turn_ref / approval_kind / prompt /
expires_at_ms`，并在可用时带 `cwd / created_at_ms / approval_detail`。
手机端 `approvalFromObject` 不需要改，但此前拿不到 `cwd`、`expires_at`，
现在可以正常显示。

### B5. Companion 能力发现可直接使用（建议接入）

`GET /cbdp/v1` 和 `GET /api/companion/ping` 的 `cbdp_v1` 字段提供
`version_major`、`version_minor`、`paths`、`auth` 等，手机端可以据此判断
老版 Bridge 是否支持 CBDP，而不必先探测 `401/404`。

## C. 双方都需要知道的协议澄清

- Companion 端口是两层鉴权：HTTP `Authorization: Bearer` 之后，会话内仍需
  `HELLO -> WELCOME -> AUTH -> AUTH_OK`。3.1.1 原先“免 AUTH”的表述已在
  v2.1.0 的 19.6 节改掉。
- `objects` / `options` / `model_capabilities` 都是 `u16 count` + 同 tag
  元素的数组；空数组是 `00 00`，字段不能省略。
- `next_cursor = 0` 表示没有下一页。
