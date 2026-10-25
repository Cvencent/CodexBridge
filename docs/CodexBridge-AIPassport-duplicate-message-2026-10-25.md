# AI Passport 交接：同一次录音被提交两次，Codex 里出现重复问句

日期：2026-10-25
状态：Bridge 侧已定性，等待 AI Passport 固件修复
影响：用户在 AI Passport 上说一句话，Codex 会话里会出现两条相同的用户消息，
设备端和桌面端看起来像“任务发了 2～3 次”。

## 结论

这不是 Codex 重复执行，也不是 Bridge 重复写入，而是**设备把同一次录音走了两条
不同的上行通道**：

1. `AUDIO_BEGIN / AUDIO_CHUNK / AUDIO_END` 语音事务，`tx_id = 350674`；
2. `SEND_BEGIN / SEND_CHUNK / SEND_END` 文本事务，`tx_id = 350675`。

两条内容的文本完全相同，但 `tx_id` 不同，因此 Bridge 按协议把它们视为两条独立
用户输入，没有任何合法依据可以合并或去重。

## 真机取证（Bridge v2.2.8，2026-10-25）

线程：`01a17355-892b-7ad0-bd7e-629a1dee56b8`（cwd `F:\aiNovelWriter`）
turn：`01a17355-b91d-7762-91ea-10dc5fbb9757`

rollout 日志
`C:\Users\vencent\.codex\sessions\2026\10\25\rollout-2026-10-25T21-52-27-01a17355-892b-7ad0-bd7e-629a1dee56b8.jsonl`
中实际落盘了两条 `UserMessage`：

| 顺序 | 时间 | `client_id` | 上行路径 | item id |
|---|---|---|---|---|
| 1 | 13:52:40 | `cbdp-audio:aipassport-98c377f55270:350674` | AUDIO_END（语音 + Bridge STT） | `01a17355-ba0e-7622-9fd5-92b05ac9113f` |
| 2 | 13:52:46 | `cbdp-tx:aipassport-98c377f55270:350675` | SEND_END（设备提交文本） | `01a17355-d179-7d50-acc3-8b95855bcbc5` |

两条文本同为 `你给我说。嗨呀？`，间隔 6 秒。

同一时间的 app-server 查询结果：

```text
turn_count = 1
turn 01a17355-b91d-7762-91ea-10dc5fbb9757 status=completed
```

也就是说：Codex 只执行了一个 turn，重复只发生在“用户输入”这一层。
设备/桌面看到的“3 条”中，多出来的一条是客户端本地乐观插入与服务端回读重复渲染，
真正写入线程的是 2 条。

## 协议依据

CBDP v1 `docs/CBDP-V1-PROTOCOL.md`：

- §19.4 `AUDIO_END`：Bridge 收到完整音频后执行 STT，并把识别结果转换为文本回复
  语义，即 `AUDIO_BEGIN / AUDIO_CHUNK / AUDIO_END -> Bridge STT ->
  SEND_BEGIN / SEND_CHUNK / SEND_END semantics -> turn.start 或 turn.steer`。
- `AUDIO_END` 的 `RESULT` 会带回 `transcript`，它是回显，不是让设备再发一次。

因此下面两种做法都是错的，都会造成重复：

1. 发完 `AUDIO_END`，又把 Bridge 返回的 `transcript` 通过 `SEND_*` 再发一遍；
2. 设备同时触发“语音上行”和“文本上行”两个回调（例如本地 STT 完成事件和录音完成
   事件都调用了发送函数），两个事务各自提交。

## 需要 AI Passport 侧修改的内容

### 1. 一次录音只允许一条上行路径

二选一，不能同时发生：

- 方案 A（推荐，现网已在用）：只发 `AUDIO_*`，由 Bridge 做 STT 并提交 turn。
- 方案 B：设备自己做好 STT，只发 `SEND_*`，完全不发 `AUDIO_*`。

如果工程上需要保留本地识别做 UI 预览，那么预览文本只能显示，不能再通过
`SEND_*` 提交一次。

### 2. `AUDIO_END` 的返回值不要再当输入发送

Bridge 在 `AUDIO_END` 的 `RESULT(0x0013)` 里返回 `TRANSCRIPT`，语义是“我识别成了
这句话，并且已经提交”。设备拿到它之后只能用于显示或状态收敛，不能再走 `SEND_*`。

### 3. 收敛发送入口，加一道本地互斥

建议在设备端录音状态机里加一个显式的“本次录音已提交”标志：

- 录音结束 → 置位并提交（只提交一次）；
- 任何重复的提交请求（第二个回调、超时重试、UI 重复点击）在标志置位后直接丢弃；
- 下一个录音周期开始（`RECORD_START` 或用户再次按下）时清除标志。

这条能同时覆盖“两个回调都发”和“超时重试又发一次”两类重复。

### 4. 重试必须复用 `tx_id` 和 `idem`

如果设备确实需要重传（例如没收到 `RESULT`），必须复用同一个 `tx_id(0x002D)` 和
同一个 `idem(0x002B)`。Bridge 对同 `tx_id` 的事务重传是按幂等处理的；换一个新的
`tx_id` 重发一定会被当成新输入。

## 不建议在 Bridge 侧做去重的原因

协议上 `turn.start` 是用户输入，不是状态。用户连续说两次同一句话（例如“继续”、
“好”）是完全合法的，Bridge 如果按文本相同去重，会把正常使用场景吃掉。所以这个
重复必须在“设备只提交一次”这一层修，Bridge 保持现状。

## 验收用例

1. 一次录音说一句话，Codex 线程里只出现 1 条 userMessage。
2. 同一个 turn 内不再出现 `cbdp-audio:` 与 `cbdp-tx:` 两条相同文本。
3. 连续两次说同一句话，Codex 里正确出现 2 条（不能被误去重）。
4. 说话后立刻再次按键录音，第二次只提交第 2 句话。
5. 弱网下 Bridge 未回 `RESULT` 时重传，仍只产生 1 条 userMessage（复用 `tx_id`/`idem`）。
6. 只开启 audio 能力、不显示语音命令入口的设备，同样只提交 1 次。

## 复测方法

1. 设备说一句可识别的短句（例如“你好呀”）。
2. 在电脑端读取对应线程的 rollout 日志，统计该 turn 的 UserMessage 数量：

```powershell
$f = Get-ChildItem "$env:USERPROFILE\.codex\sessions" -Recurse -Filter '*<thread_id>*.jsonl' |
     Sort-Object LastWriteTime -Descending | Select-Object -First 1
Select-String -LiteralPath $f.FullName -Pattern 'UserMessage' |
  Where-Object { $_.Line -match 'cbdp-' }
```

3. 期望：只出现一条 `UserMessage`，且 `client_id` 只有 `cbdp-audio:` 或
   `cbdp-tx:` 之一。

## 相关代码位置（Bridge 侧，供对照，不需要修改）

- `codexbridge/cbdp/gateway.py` `_handle_send_end`：文本事务 -> `turn.start`，
  `request_id = cbdp-tx:<device_id>:<tx_id>`
- `codexbridge/cbdp/gateway.py` `_handle_audio_end`：语音事务 -> STT -> `turn.start`，
  `request_id = cbdp-audio:<device_id>:<tx_id>`
- `codexbridge/companion/service.py` `start_turn`：把 `client_message_id` 写入
  `clientUserMessageId`，因此上面两条 id 会直接出现在 rollout 里
