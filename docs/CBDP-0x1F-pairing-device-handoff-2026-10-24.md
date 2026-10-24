# CBDP `PAIRING(0x1F)` 设备端接入说明

- 日期：2026-10-24
- 接收方：AI Passport / ESP32 设备固件开发者
- Bridge 侧状态：已实现并提交，Bridge 提交 `c062200`
- 正式协议：`docs/CBDP-V1-PROTOCOL.md` 第 7.1、8.10、11.8、19.16 节
- 适用设备：没有 USB、不能使用旧 JSON `register_offer`、只走 CBDP 的 ESP32 /
  ESP32-S3 设备

> 本文是设备端实现清单。旧 JSON 设备、手机端和已有令牌的设备**不需要**改。
> 设备只要在“没有本地令牌”时多走一次 `PAIRING` 请求，其余 CBDP 流程不变。

---

## 1. 这次新增了什么

| 项 | 值 | 方向 |
|---|---:|---|
| 消息类型 `PAIRING` | `0x1F` | Bridge 到设备 |
| TLV `pairing_state` | `0x00C0` | HELLO / PAIRING |
| TLV `pairing_token` | `0x00C1` | PAIRING |
| `PAIRING_STATE_NONE` | `0` | 设备不需要发送 |
| `PAIRING_STATE_REQUEST` | `1` | 设备在 HELLO 中发送 |
| `PAIRING_STATE_OFFER` | `2` | Bridge 在 PAIRING 中发送 |

`pairing_token` 按 UTF-8 编码，长度必须是 **16..64 字节**。设备存进 NVS 后，
后续 AUTH 仍使用原来的 HMAC-SHA256，不存在第二套鉴权算法。

## 2. 设备端要改的四步

设备仓库当前相关文件：

```text
firmware/main/codex_bridge_protocol.h
firmware/main/codex_bridge_protocol.c
firmware/main/wireless_bridge.c
```

### 第一步：在协议头里加常量

在 `codex_bridge_protocol.h` 的 `cbdp_message_type_t`、TLV 枚举和状态枚举中加：

```c
/* cbdp_message_type_t */
CBDP_MT_PAIRING = 0x1F,

/* TLV 枚举 */
CBDP_TLV_PAIRING_STATE = 0x00C0,
CBDP_TLV_PAIRING_TOKEN = 0x00C1,

/* 配对状态 */
enum {
    CBDP_PAIRING_STATE_NONE = 0,
    CBDP_PAIRING_STATE_REQUEST = 1,
    CBDP_PAIRING_STATE_OFFER = 2,
};
```

### 第二步：HELLO 按是否需要令牌携带 `pairing_state`

设备当前在 `transmit_hello()` 里调用 `cbdp_build_hello()`。把签名改成可选的
`bool request_pairing`，并只在没有 token 时写 `0x00C0`：

```c
bool cbdp_build_hello(uint8_t *buffer, size_t capacity, size_t *length,
                      uint32_t request_id, const uint8_t client_nonce[16],
                      bool request_pairing)
{
    if (!begin_frame(buffer, capacity, length, CBDP_MT_HELLO,
                     CBDP_FLAG_REQUEST, CBDP_QOS_CRITICAL, 0U, 0U)) {
        return false;
    }
    if (!cbdp_tlv_write_u16(buffer, capacity, length, CBDP_TLV_VERSION_MINOR,
                            CBDP_VERSION_MINOR) ||
        /* ...原来已有的 device_id/model/firmware/name/profile/caps/TLS... */
        !cbdp_tlv_write_u32(buffer, capacity, length, CBDP_TLV_REQUEST_ID,
                            request_id)) {
        return false;
    }
    if (client_nonce != NULL &&
        !cbdp_tlv_write_bytes(buffer, capacity, length, CBDP_TLV_CLIENT_NONCE,
                              client_nonce, 16U)) {
        return false;
    }
    if (request_pairing &&
        !cbdp_tlv_write_u8(buffer, capacity, length, CBDP_TLV_PAIRING_STATE,
                           CBDP_PAIRING_STATE_REQUEST)) {
        return false;
    }
    return finish_frame(buffer, capacity, length, 0U);
}
```

调用点只有一个，改成：

```c
const bool request_pairing = (s_token[0] == '\0');
if (!cbdp_build_hello(frame, sizeof(frame), &length, s_request_id++,
                      s_client_nonce, request_pairing)) {
    return false;
}
```

**兼容性**：已经有 token 的设备 `request_pairing=false`，不写 `pairing_state`，
Bridge 行为与旧固件完全一致。

### 第三步：解析 `PAIRING`，保存 token，再 AUTH

设备当前 `process_client_frame()` 收到 `WELCOME` 后立刻
`transmit_auth()`；无 token 时 `transmit_auth()` 返回 false，直接断线。

改成：

```text
收到 WELCOME：
  parse WELCOME
  if (s_token[0] != '\0') {
      transmit_auth()                  // 旧流程，完全不变
  } else {
      保持连接，等待 PAIRING        // 不要在这里 AUTH，更不要断线
  }

收到 PAIRING：
  if (frame 未认证之前也必须允许处理)
  state = TLV(0x00C0)                  // 必须是 OFFER(2)
  token = TLV(0x00C1)                  // UTF-8，16..64 字节
  保存到 NVS
  transmit_auth()                      // 标准 HMAC AUTH
```

建议在 `codex_bridge_protocol.c` 增加一个只解析 PAIRING 的函数，例如：

```c
bool cbdp_parse_pairing_offer(const uint8_t *tlv, size_t length,
                              char *token_out, size_t token_capacity)
{
    const cbdp_tlv_t *state = cbdp_tlv_find(tlv, length,
                                            CBDP_TLV_PAIRING_STATE);
    uint8_t value = 0;
    if (!cbdp_tlv_read_u8(state, &value) ||
        value != CBDP_PAIRING_STATE_OFFER) {
        return false;
    }
    const cbdp_tlv_t *token = cbdp_tlv_find(tlv, length,
                                            CBDP_TLV_PAIRING_TOKEN);
    const uint8_t *bytes = NULL;
    size_t byte_length = 0;
    if (token == NULL ||
        !cbdp_tlv_read_bytes(token, &bytes, &byte_length) ||
        byte_length < 16U || byte_length > 64U ||
        token_capacity <= byte_length) {
        return false;
    }
    memcpy(token_out, bytes, byte_length);
    token_out[byte_length] = '\0';
    return true;
}
```

在 `process_client_frame()` 的 `WELCOME` 分支之后、认证检查之前增加：

```c
if (header.message_type == CBDP_MT_PAIRING) {
    char new_token[TOKEN_MAX + 1];
    if (!cbdp_parse_pairing_offer(
            header_tlv, header.header_len - CBDP_FIXED_HEADER_BYTES,
            new_token, sizeof(new_token))) {
        client_replace(-1);
        return;
    }
    if (save_token(new_token) != ESP_OK) {
        client_replace(-1);
        return;
    }
    strlcpy(s_token, new_token, sizeof(s_token));
    if (!transmit_auth(s_client_fd)) {
        client_replace(-1);
    }
    return;
}
```

`PAIRING` 发生在 AUTH 之前，所以设备必须像 WELCOME 一样在“未认证”状态处理它。
不要把它放到 `if (!authenticated) return;` 后面，否则永远收不到。

### 第四步：处理“尚未批准”

ESP 是 TCP 监听方（Device Listener Binding），**未批准时 Bridge 不会拨号**。
因此正常的首次接入顺序是：

1. 设备继续周期广播 UDP presence（`DISCOVERY_REPLY`），让管理页出现“待批准”；
2. 用户在管理页点“批准”，Bridge 生成 token 并写入注册表；
3. Bridge 主动连接设备 8765；
4. 设备按第二步发 `HELLO(pairing_state=request)`，拿到 `PAIRING` 令牌。

如果走的是 Bridge 监听绑定（例如手机 Companion HTTP/WebSocket），或者已批准
但注册表里还没有令牌，Bridge 会回：

```text
ERROR(0x0F)
  code = POLICY_DENIED(0x0010)
  error_message = "PAIRING_PENDING"
  error_detail.reason = "pairing_pending"
```

设备此时不要频繁重连。建议：

1. 关闭当前 CBDP 连接；
2. 按退避重新 HELLO，例如 `2s -> 4s -> 8s -> 16s -> 30s`；ESP 监听绑定下
   则是继续周期广播 presence，等 Bridge 批准后拨号；
3. 管理员批准后，Bridge 会在下一次 HELLO 中回 `PAIRING` 并下发 token；
4. token 保存并 AUTH 成功后，清空退避计数。

如果设备支持显示状态，建议显示“等待电脑端批准”，不要显示成“连接失败”。

## 3. 精确时序

### 3.1 管理员已批准

```text
Device                                Bridge
  |-- HELLO(pairing_state=request) ---->|
  |<--------------- WELCOME -------------|
  |<-- PAIRING(offer, token=<16..64>) --|
  |-- AUTH(HMAC pairing_token) --------->|
  |<----------------- AUTH_OK -----------|
  |-- SUBSCRIBE / COMMAND ... ---------->|
```

`PAIRING.request_id` 必须等于触发它的 HELLO 的 `request_id`。`PAIRING` 带
`FLAG_RESPONSE`，设备不需要给它回 ACK。

### 3.2 管理员还没批准（ESP 监听绑定）

```text
Device                                Bridge
  |-- UDP DISCOVERY_REPLY(presence) --->|  管理页出现“待批准”
  |          （Bridge 不拨号 8765）       |
  |                                     |
  | 用户点“批准”，Bridge 生成 token       |
  |<======== Bridge 主动 connect 8765 ====|
  |-- HELLO(pairing_state=request) ---->|
  |<--------------- WELCOME -------------|
  |<-- PAIRING(offer, token) -----------|
```

也就是说：**未批准时不会走到 HELLO，也不会收到 `PAIRING_PENDING`**。设备要做
的是保持 UDP presence 周期广播，不要因为没连上而停止广播。

### 3.3 设备已有 token（旧固件行为）

```text
Device                                Bridge
  |-- HELLO(无 pairing_state) ---------->|
  |<--------------- WELCOME -------------|
  |-- AUTH(HMAC 已有 token) ------------>|
  |<----------------- AUTH_OK -----------|
```

Bridge 在 HELLO 未携带 `pairing_state` 时不会发送 `PAIRING`。

## 4. 字节示例

下面是“已批准”场景中的一帧 `PAIRING`，token 为
`0123456789abcdef`（仅用于展示，真实 token 由 Bridge 随机生成）：

```text
CB 01 01 1F 02 00 00 3D 00 00 00 00 00 00 00 00 00 00 00 02 00 00 00 01
00 00 02 00
00 2A 00 04 00 00 00 07
00 C0 00 01 02
00 C1 00 10 30 31 32 33 34 35 36 37 38 39 61 62 63 64 65 66
```

字段解释：

```text
message_type = 0x1F
flags        = 0x0200 (RESPONSE)
seq          = 2
ack_seq      = 1
qos          = 2 (telemetry)
request_id   = 0x00000007
pairing_state= 0x02 (OFFER)
pairing_token= "0123456789abcdef"
```

真实 token 长度可能不同，设备必须按 `0x00C1` 的实际 TLV 长度读取，不能假设
固定字符串长度。

## 5. 令牌存储与安全

- 只保存到 NVS 安全存储或等价受保护区域；设备现有
  `NVS_NAMESPACE="folo_bridge"`、`NVS_TOKEN_KEY="token"` 可直接复用。
- 保存后不要修改、截断或重新编码 token；AUTH HMAC 用的是原始 UTF-8 字节。
- 不得把 token 写入普通日志、遥测、错误上报或屏幕调试信息。
- 收到 `UNAUTHORIZED(0x0003)` / `TOKEN_ROTATED(0x0018)` 时，清除本地 token，
  下一次 HELLO 重新带 `pairing_state=request`。
- 撤销设备后，Bridge 会清空 token；旧 token 的 AUTH 必须失败，不能继续使用。

## 6. 绝对不要做的事

1. 不要在收到 WELCOME 后无条件立刻 AUTH。无 token 时必须等 PAIRING。
2. 不要发送 PAIRING。该消息 v1 只有 Bridge 到设备方向。
3. 不要在 8765 上解析 JSON 或等待 register_offer。纯 CBDP 设备不需要它。
4. 不要在 HELLO 前阻塞 peek/recv。必须有设备先发 HELLO。
5. 不要把 pairing_state 写进已有 token 的设备。直接走旧 AUTH 即可。
6. 不要用 token 长度当固定值。
7. 不要在未认证状态过滤掉 PAIRING。

## 7. 验收用例

### 用例 1：首次无线配对

1. 清空设备 NVS token；
2. 确认设备广播 presence 后，Bridge 管理页出现“待批准”；
3. 确认 Bridge 此时不会连接设备 8765；
4. 在管理页点“批准”；
5. 确认 Bridge 主动连接设备 8765，设备发出 pairing_state=request 的 HELLO；
6. 确认设备收到 PAIRING，保存 token 后发出 AUTH 并收到 AUTH_OK；
7. 拔电重启设备，确认 HELLO 不再携带 pairing_state，直接 AUTH 成功。

### 用例 2：旧流程回归

1. 设备已有有效 token；
2. 确认 HELLO 不携带 0x00C0；
3. 确认没有收到 PAIRING；
4. 确认 AUTH / AUTH_OK 与升级前一致。

### 用例 3：撤销与重新配对

1. 设备已正常在线；
2. 在 Bridge 管理页撤销该设备；
3. 确认当前连接被关闭、旧 token 不再可用；
4. 设备清除本地 token 后重新 HELLO；
5. 重新批准后确认能再次拿到 token 并上线。

### 用例 4：安全与日志

1. 在设备普通日志中搜索 token 字符串，必须为空；
2. 在错误上报、遥测和 UI 调试输出中搜索 token，必须为空；
3. 用 16 字节和 64 字节两种 token 各跑一次，必须都能保存并 AUTH 成功。

## 8. Bridge 侧已具备的条件

设备端联调时可以直接依赖以下 Bridge 行为：

- HELLO 带 pairing_state=request 且设备已批准、注册表已有 token 时，
  Bridge 在 WELCOME 之后发送 PAIRING；
- 设备已批准但注册表没有 token 时，Bridge 回 PAIRING_PENDING；
- 设备未批准时，ESP 监听拓扑下 Bridge 不拨号，管理页显示“待批准”；
  Bridge 监听拓扑下则回 `PAIRING_PENDING`；
- 纯 CBDP 设备没有旧 JSON registration_nonce 时，管理页“批准”会直接生成
  本机 token，并写入统一设备注册表；
- HELLO 未带 pairing_state 时，Bridge 不发 PAIRING，旧设备行为不变。

## 9. 联调记录建议

真机联调时请让设备日志至少包含以下字段，便于 Bridge 侧核对：

```text
pairing request: request_id=<id> token_present=<0/1>
pairing pending: retry_in_ms=<n>
pairing offer: request_id=<id> token_bytes=<16..64>
pairing saved: nvs=ok
CBDP session ready
```

不要打印 token 本身。

## 10. 参考

- 正式协议：docs/CBDP-V1-PROTOCOL.md
  - 7.1 消息类型
  - 8.10 发现、分片与发现相关 TLV
  - 11.2 HELLO
  - 11.8 无线配对
  - 19.16 无线配对消息（2026-10-24 后续补充）
- 管理接口：docs/companion-and-admin-api.md
- Bridge 实现提交：c062200 feat: add CBDP 0x1F wireless pairing
