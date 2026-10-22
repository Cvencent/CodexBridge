# 设备端缺陷说明：手机 App 长连接滥用导致 PC 卡顿

- 日期：2026-10-22
- 受影响 PC 端：CodexBridge 2.1.33（Companion 端口 8568，CBDP v1 已启用）
- 受影响设备：Xiaomi 24129PN74C（192.168.0.45），phone_id `phone-051b642ae212`
- 结论：**PC 卡顿的主因在设备端**。设备在一段时间内持续新建并持有大量长连接，
  每条连接在 PC 侧占用一个阻塞线程，线程数涨到 500 以上后整机输入延迟明显上升。
  本文只描述需要设备侧修复的问题，PC 侧已另做资源保护（见文末）。

---

## 1. 现场证据

在 CodexBridge 进程（PID 30372）上采样：

| 指标 | 观测值 |
| --- | --- |
| TCP 8568 ESTABLISHED | 约 467–490，其中几乎全部来自 192.168.0.45 |
| 连接变化 | 每 5 秒新建 65–86 条、消失 62–86 条（约 13–16 条/秒 churn） |
| 进程线程数 | 501–512，且随时间增长 |
| `py-spy dump` 采样 | 总线程 509，其中 `_cbdp_sse` **376 条**、`_cbdp_websocket` 35 条 |
| CPU | 线程几乎都是 `idle`，不是 CPU 打满 |

复现采样命令（在 PC 上执行，替换 `<PID>`）：

```powershell
Get-NetTCPConnection -OwningProcess <PID> -State Established |
  Group-Object RemoteAddress,RemotePort |
  Sort-Object Count -Descending | Select-Object -First 20 Count,Name
```

关键点：堆积的不是 CPU 计算，而是**线程与连接数量本身**。每个长连接在
CodexBridge 里对应一个阻塞服务线程，数量达到数百后，整机调度与输入响应变差，
表现为「开着 CodexBridge 鼠标就卡，关掉就不卡」。

---

## 2. 问题一：同一设备并行开启了数百条 `/cbdp/v1/events`（SSE）

`py-spy` 中 376 条 `_cbdp_sse` 线程全部停在
`codexbridge/companion/api.py` 的 `session.queue.get(timeout=1.0)`，
对应路由 `/cbdp/v1/events`（CBDP v1 事件流）。

同一台 192.168.0.45 设备在**同一时刻**并发持有数百条该 SSE 流，且长期不主动关闭。
CBDP 服务端协议约定每台设备复用单条长连接即可获知全部事件，并行开流既不必要，
也会在任意实现里产生线程/连接放大。

### 设备侧要求

1. 全局只保留 **1 条** CBDP 事件长连接（SSE 或 WebSocket 二选一）。
2. 断线后重连必须退避，并**先释放旧连接**，禁止「不断新建、旧连接不关」。
3. 对于 `GET /cbdp/v1/events` 与 `/cbdp/v1/ws`：同一进程/设备只允许一个
   活动连接对象，切换 thread/过滤条件时复用同一条连接或显式关闭后再开。

---

## 3. 问题二：手机 App 每次创建 `ApiClient` 都新建 OkHttpClient

`mobile/android/app/src/main/java/com/codexbridge/mobile/data/ApiClient.kt:45`：

```kotlin
class ApiClient(baseUrl: String, private val token: String? = null) {
    private val client = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        // ...
        .build()
}
```

`OkHttpClient` 是**实例字段**，因此每个 `ApiClient` 实例各自持有一个独立连接池。
而 `ApiClient` 在代码中被反复新建：

- `AppViewModel.kt:193`、`212`、`220`、`279`、`282`、`302`
- `CompletionNotificationService.kt:170`、`175`、`177`

OkHttp 官方要求全应用共享一个 `OkHttpClient` 实例；按实例创建会让连接池无法复用，
表现为连接数随调用点增加而膨胀。

### 设备侧要求

1. 全应用共享**单个** `OkHttpClient`（连接池、线程池、Dispatcher 复用）。
2. `ApiClient` 只持有 base URL/token 等数据，不持有独立 client。
3. 轮询服务与前台 ViewModel 复用同一个 client 实例。

---

## 4. 问题三：SSE 断线后立即重连，无退避

`AppViewModel.kt:564-582`：

```kotlin
private fun observeThread(threadId: String) {
    streamJob = viewModelScope.launch {
        while (true) {
            client.events(threadId).collect { event -> /* ... */ }
            if (streamFailed || !_state.value.connected) refreshConnectionRoute()
            delay(3000)                       // 固定 3 秒，无退避
        }
    }
}
```

失败后固定 3 秒重连。当 PC 端或网络抖动时，多台设备/多个入口会同步以固定节奏
重连，形成稳定高频的连接 churn。

### 设备侧要求

1. 重连使用指数退避并加抖动（例如 1s→2s→4s→8s，上限 30s，±20% 抖动）。
2. 重连前确保上一条 SSE/WS 已 `cancel()`，不得泄漏旧连接。
3. `onFailure` 收到 HTTP 429 时必须读取并遵循 `Retry-After`（PC 侧现为 20 秒）。

---

## 5. 问题四：多个固定周期轮询叠加

| 位置 | 周期 | 说明 |
| --- | --- | --- |
| `AppViewModel.kt:1181` | 2000 ms | 线程列表刷新（`THREADS_REFRESH_MS`） |
| `AppViewModel.kt:586` | 4000 ms | 当前线程详情轮询 |
| `CompletionNotificationService.kt:42` | 5000 ms | 完成通知轮询（`POLL_MS`） |
| `AppViewModel.kt:580` | 3000 ms | SSE 固定重连间隔 |

这些轮询在多个界面/服务里各自运行，且每个新 `ApiClient` 使用独立连接池，
叠加后造成持续的高频请求与连接建立。

### 设备侧要求

1. 前台轮询收敛到单一调度器，按当前可见界面合并请求。
2. 后台/前台切换时暂停不必要的轮询。
3. 尽量用长连接推送替代高频轮询。

---

## 6. 回复 PC 端 429 的约定（PC 侧已实现）

从 CodexBridge 本次修复起，长连接按设备限额：

- 单设备合计最多 **6** 条长流：SSE **2**、WebSocket **2**、Companion events **2**
- 全服务合计最多 **24** 条
- 超限响应：

```
HTTP/1.1 429 Too Many Requests
Retry-After: 20
Connection: close

{"ok": false, "error": "too_many_streams"}
```

`server_stream_limit` 表示触达全局上限。设备收到 429 后应：

1. 不再对该端点发起新的并行连接；
2. 等待 `Retry-After` 后重试，且重试时复用已有连接；
3. 检查并关闭本进程里已泄漏的旧 SSE/WS。

---

## 7. 验证清单（设备侧修复后需自测）

- [ ] 全进程 OkHttpClient 实例数为 1。
- [ ] 同一设备对 `/cbdp/v1/events` 的活动连接数始终为 1。
- [ ] 断开网络 30 秒再恢复，重连次数随退避递减，不出现每秒多次重连。
- [ ] 收到 429 后停止新建连接，并观察到重试间隔 ≥ `Retry-After`。
- [ ] 连续运行 30 分钟后，PC 端
      `Get-NetTCPConnection -OwningProcess <PID> -State Established` 中来自该设备
      的连接数稳定在个位数，CodexBridge 线程数稳定（不随时间增长）。
