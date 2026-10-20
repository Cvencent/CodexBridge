# CodexBridge 手机端附件接入说明

日期：2026-10-20
适用版本：CodexBridge `2.1.15` 及以上
目标读者：CodexBridgeMobile 手机端开发者

本文档描述手机端如何通过 Companion HTTP 接口上传图片、附件，并把它们随消息一起
发送给 Codex。CBDP 二进制设备使用同一套存储与命令层，方法号见文末。

## 1. 能力声明

手机配对后默认启用 `attachment` 能力。请先确认能力清单里包含它：

```http
POST /api/companion/capabilities
Authorization: Bearer <token>
Content-Type: application/json

{"supported": ["status", "tasks", "control", "approval", "queue", "attachment"]}
```

响应：

```json
{"ok": true, "enabled": ["approval", "attachment", "control", "queue", "status", "tasks"]}
```

如果没有声明 `attachment`，上传接口返回 `403 capability_not_enabled`。

## 2. 上传流程总览

附件上传分三步，内容和列表都按线程隔离：

1. `POST /api/companion/threads/<thread_id>/attachments` 开始上传，拿到
   `upload_id`。
2. `POST /api/companion/uploads/<upload_id>/chunk` 按 `offset` 顺序上传分片，
   分片内容是 base64。
3. `POST /api/companion/uploads/<upload_id>/end` 完成上传，拿到
   `attachment_id` 和 `content_ref`。

完成后把 `attachment_id` 放进发消息请求的 `input_parts`，Bridge 会把它翻译成
Codex 的图片输入。

### 2.1 约束

| 项 | 值 |
| --- | --- |
| 单个附件上限 | 32 MiB |
| 单个分片上限 | 2 MiB（解码后） |
| 支持的 `kind` | `file`、`image`、`audio`、`other` |
| 图片 MIME | 必须 `image/*` |
| 每个线程保留数量 | 最近 64 个，超出后淘汰最旧的 |
| 上传事务有效期 | 1 小时 |

`mime_type` 与 `kind` 必须匹配：把 `text/plain` 声明成 `image` 返回
`415 ATTACHMENT_KIND_MISMATCH`。`content_sha256` 是可选字段；提供时会校验，
不匹配返回 `422 CONTENT_HASH_MISMATCH`。

## 3. 开始上传

```http
POST /api/companion/threads/01a157bc-a656-7961-a40a-a9bdfd0f790e/attachments
Authorization: Bearer <token>
Content-Type: application/json

{
  "kind": "image",
  "byte_size": 348123,
  "content_sha256": "可选，64 位小写十六进制",
  "filename": "photo.png",
  "mime_type": "image/png",
  "alt_text": "可选，图片说明"
}
```

成功返回 `201`：

```json
{
  "ok": true,
  "upload_id": "u_1f0c9a...",
  "thread_id": "01a157bc-...",
  "upload_state": "pending",
  "upload_offset": 0,
  "byte_size": 348123
}
```

## 4. 上传分片

```http
POST /api/companion/uploads/u_1f0c9a.../chunk
Authorization: Bearer <token>
Content-Type: application/json

{
  "offset": 0,
  "data": "iVBORw0KGgoAAAANSUhEUg..."
}
```

`data` 是原始字节的 base64 编码，`offset` 必须等于服务端当前
`upload_offset`。乱序返回 `409 UPLOAD_OFFSET_MISMATCH`，此时请重新从服务端返回
的 `upload_offset` 续传。

成功返回：

```json
{"ok": true, "upload_id": "u_1f0c9a...", "upload_state": "uploading", "upload_offset": 524288, "byte_size": 348123}
```

建议分片大小 256 KiB 到 1 MiB，兼顾弱网重试与请求开销。

## 5. 完成或取消上传

完成：

```http
POST /api/companion/uploads/u_1f0c9a.../end
Authorization: Bearer <token>
Content-Type: application/json

{"byte_size": 348123}
```

成功返回：

```json
{
  "ok": true,
  "attachment_id": "att_9d1c...",
  "attachment_ref": "att_9d1c...",
  "thread_id": "01a157bc-...",
  "kind": "image",
  "filename": "photo.png",
  "mime_type": "image/png",
  "byte_size": 348123,
  "content_sha256": "9d1c...",
  "content_ref": "content:att_9d1c...",
  "upload_state": "complete"
}
```

取消：

```http
POST /api/companion/uploads/u_1f0c9a.../cancel
Authorization: Bearer <token>
Content-Type: application/json

{}
```

## 6. 带附件发消息

在原有发消息接口上增加 `input_parts`。`text` 与 `input_parts` 可以同时存在，
`input_parts` 的顺序就是模型看到的输入顺序：

```http
POST /api/companion/threads/01a157bc-.../messages
Authorization: Bearer <token>
Content-Type: application/json

{
  "text": "帮我看看这张图",
  "input_parts": [
    {"type": "text", "text": "帮我看看这张图"},
    {"type": "image", "attachment_id": "att_9d1c..."}
  ],
  "client_message_id": "可选，幂等 ID"
}
```

字段说明：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `type` | string | 是 | `text` 或 `image` |
| `text` | string | `type=text` 时必填 | 文本内容 |
| `attachment_id` | string | `type=image` 时必填 | 第 5 步返回的 ID |
| `content_ref` | string | 否 | 也可用 `content:att_...` |

只发图片不带文字也可以，此时省略 `text`。未上传完成的 `attachment_id` 返回
`400 attachment_not_found`。

发送后 Bridge 会把图片转成 Codex 内联输入（`input_image` + data URL），
手机端不需要自己构造 base64 输入结构。

## 7. 查看与读取附件

列出某线程的附件：

```http
GET /api/companion/threads/<thread_id>/attachments
```

```json
{"ok": true, "attachments": [{"attachment_id": "att_...", "kind": "image", "filename": "photo.png", "byte_size": 348123, "content_ref": "content:att_..."}], "object_count": 1}
```

按需分页读取内容（返回 base64）：

```http
GET /api/companion/attachments/<attachment_id>?offset=0&max_bytes=262144
```

```json
{"ok": true, "attachment_id": "att_...", "offset": 0, "next_offset": 262144, "total_bytes": 348123, "content_base64": "iVBORw0..."}
```

删除附件：

```http
POST /api/companion/attachments/<attachment_id>
Authorization: Bearer <token>
Content-Type: application/json

{"action": "remove"}
```

## 8. 错误码

| HTTP | `error` | 含义 |
| --- | --- | --- |
| 400 | `INVALID_PARAM` / `invalid_body` / `invalid_request` | 缺字段、分片为空、参数类型错误或 JSON 无法解析 |
| 403 | `capability_not_enabled` | 手机未声明 `attachment` 能力 |
| 404 | `NOT_FOUND` | 上传事务或附件不存在 |
| 409 | `UPLOAD_OFFSET_MISMATCH` | 分片 offset 与当前进度不一致 |
| 409 | `UPLOAD_INCOMPLETE` | 完成上传时字节数不足 |
| 413 | `PAYLOAD_TOO_LARGE` | 超过 32 MiB 或单分片上限 |
| 415 | `ATTACHMENT_KIND_MISMATCH` | `kind` 与 MIME 不匹配 |
| 422 | `CONTENT_HASH_MISMATCH` | SHA-256 校验失败 |

## 9. 本地数据与清理

Bridge 把内容保存在：

```text
%LOCALAPPDATA%\CodexBridge\attachments\
```

`index.json` 是附件索引，`att_*.bin` 是内容文件。上传中的分片是
`.upload-u_*`，完成后会被移除或重命名。请勿让手机直接读写这些文件，只用上面的
HTTP 接口。

## 10. CBDP 设备方法号

ESP/面板类设备走 CBDP 时使用同一份存储与命令层：

| 方法 | ID |
| --- | --- |
| `attachment.add` | `0x0060` |
| `attachment.list` | `0x0061` |
| `attachment.remove` | `0x0062` |
| `attachment.upload.begin` | `0x0063` |
| `attachment.upload.chunk` | `0x0064` |
| `attachment.upload.end` | `0x0065` |
| `attachment.upload.cancel` | `0x0066` |
| `content.read` | `0x00C1` |

TLV 编号与字段语义见 [CBDP-V1-PROTOCOL.md](CBDP-V1-PROTOCOL.md) 第 8.11.4 节和
第 17.11 节。`turn.start`（`0x0005`）与 `queue.add`（`0x0030`）的 `input_parts`
使用 `0x015B` 数组，元素 tag 为 `0x00A0`。

## 11. 验收用例

1. 上传一张 1 MiB 以内的 PNG，完成上传得到 `attachment_id`。
2. 发送带该图片的消息，Codex 能收到图片并能描述内容。
3. 只发图片、不带文字，仍能正常发送。
4. 上传中途退出 App，重新进入后能从返回的 `upload_offset` 续传。
5. 故意断电重连后，已完成附件仍出现在线程附件列表里且可再次发送。
6. 上传一个 MIME 与 `kind` 不符的文件，返回 `ATTACHMENT_KIND_MISMATCH`。
7. 发送一个不存在的 `attachment_id`，返回 `attachment_not_found`。
