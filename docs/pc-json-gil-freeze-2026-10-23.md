# PC 侧根因与修复：超大线程 JSON 冻结进程（2026-10-23）

- 日期：2026-10-23
- 受影响版本：CodexBridge 2.1.33 及更早
- 现象：CodexBridge 一启动，整机鼠标就卡；关闭 CodexBridge 立刻恢复
- 结论：**与设备无关，是 PC 侧读取超长 Codex 线程时解析超大 JSON 导致的。**
  设备团队无需修改任何代码。

---

## 1. 现象与排除过程

用户报告「鼠标一移动就卡」，且所有设备关闭后依旧卡顿，关闭 CodexBridge 后
立即恢复正常。

复测结果：

| 条件 | 结果 |
| --- | --- |
| 全部设备关闭，TCP 8568 ESTABLISHED | 0 |
| CodexBridge 运行 | 仍然卡顿 |
| CodexBridge 关闭 | 卡顿消失 |

因此可以排除设备、局域网连接、TCP 会话数量等设备侧因素，问题锁定在
CodexBridge 自身的 PC 侧执行路径上。

## 2. 根本原因：单行几十 MB 的 JSON 解析长时间持有 GIL

CodexBridge 读取 Codex 桌面线程详情时，原先调用：

```json
{"method": "thread/read", "params": {"threadId": "...", "includeTurns": true}}
```

`includeTurns: true` 会让 app-server 把该线程**全部历史 turn 及其全部 item**
序列化成**一行 JSON**返回。实测数据（长线程
`01a15341-4afd-7f01-9a56-380ddc70bac9`，70 turns）：

| 读法 | 返回体量 | 耗时 |
| --- | --- | --- |
| `thread/read` + `includeTurns: true` | **84.7 MB**（单行） | 约 2.0 s |
| `thread/read` + `includeTurns: false` | 约 1.5 KB | 毫秒级 |
| `thread/turns/list` + `itemsView: summary` | 309 KB | 约 0.1 s |
| `thread/items/list` 单页（limit=200, full） | 200–510 KB | 毫秒级 |

Python 的 `json.loads()` 是 C 实现，但解析期间会**长时间持有 GIL**。在 CPython
里，GIL 意味着整个进程里所有 Python 线程都被冻结。CodexBridge 是单进程多线程
架构（设备会话、Companion API、collector 都在同一进程），一次 2 秒级的
`json.loads` 会让所有线程停摆，表现为整个系统输入响应变差——也就是鼠标卡顿。

另外，`includeTurns: true` 针对整条线程一次性拉全量，数据量与线程长度**线性增长**；
使用越久的长线程越容易触发，卡顿也越明显。

## 3. 修复方案（仅 PC 侧，未改协议）

把两条读取路径都从「整线程一次性拉全量」改为「有界分页读取」：

1. **collector 侧**（`codexbridge/codex/app_server.py`）
   - `thread/read` 只取线程元数据（`includeTurns: false`）。
   - `thread/turns/list` 取一页 turn 摘要（`itemsView: summary`，上限 100）。
   - 只对最新 turn 用 `thread/items/list` 取有界头部与尾部（limit=200，最多 2 页），
     合并去重后作为该 turn 的 items。
2. **companion 侧**（`codexbridge/companion/service.py`）
   - 同样改为 metadata + turns 分页 + 最新 turn 的有界 items。
   - `thread/items/list` 纳入只读短缓存，避免每次状态轮询重复拉取分页。
3. **防御性上限**（`codexbridge/codex/app_server.py`）
   - app-server stdout 单行长度超过 8,000,000 字符时丢弃并排空到换行，不再交给
     `json.loads`，避免任何单条超大响应再次冻结进程。

## 4. 兼容性与数据正确性

- `thread/turns/list` 的 `itemsView: summary` 返回的每个 turn 都带 items
  （含 `userMessage`、`agentMessage`），因此 `user_message_count` 等统计不退化。
- `token_count` 在本修复之前就一直是 0（原 `thread/read` 响应里本来没有
  `usage` / `tokenUsage` 字段），不是本次改动引入的退化。
- 只对最新 turn 补齐完整 items，与设备实际展示需求一致：设备关注的是当前
  进行中的 turn 与最新回复。
- **未修改 CBDP 线协议**：本次只改 PC 侧读取方式与缓存策略，协议字段、消息
  类型、端口与握手均未变化，设备端无需同步升级。

## 5. 验证

- `python -m unittest discover -s tests -v`：823 tests，全部通过。
- 涉及套件：`tests.test_companion_service`（119）、`tests.test_rollout_liveness`
  （17）、`tests.test_companion_read_cache`（11）、`tests.test_companion_api`（28）、
  `tests.test_app_server_stdout`（1）。
- 新增测试覆盖：超大 stdout 行被丢弃且不影响后续消息解析；分页读取路径
  不退化且被缓存。

## 6. 给设备团队的结论

**本次卡顿不需要设备端做任何修改。** 这是 CodexBridge PC 侧的读取路径问题，
已在 PC 侧修复。设备端（手机 App、固件）无需为本次问题提交改动、调整轮询
节奏或修改连接策略。

2026-10-22 记录的长连接堆积是另一个独立的、PC 侧已做限额保护的问题，
详见 [设备端建议说明](device-defect-long-lived-connections-2026-10-22.md)。
