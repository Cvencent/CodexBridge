# 手机端交接：Bridge 换 IP 后不应重新配对

日期：2026-10-26
状态：Bridge 侧已提供稳定实例码，等待手机端接入
影响：电脑 IP 从 `192.168.0.47` 变为 `192.168.0.49` 后，手机仍使用旧地址，
弹出“令牌配对失败”，但 Bridge 中的配对记录和令牌仍然存在。

## 结论

Bridge 的令牌鉴权只校验 Bearer token 的 SHA-256 摘要，不校验来源 IP。换 IP 本身
不会让令牌失效。

真正的问题是手机把 `baseUrl`（IP + 端口）当成持久连接身份：

- `settings.baseUrl` / `lanUrl` / `lastSuccessfulUrl` 仍指向旧 IP；
- 已配对后手机不会用发现结果更新地址；
- 旧 IP 被其它主机占用时返回 `401`，手机把它显示成“令牌配对失败”。

## Bridge 侧已修改

Bridge 现在持久化一个 32 位十六进制 `bridge_instance_id`，并派生稳定
`device_id`（`bridge-<12位>`）。它保存在 `companion.json`，不会随 IP 或端口变化。

以下响应已包含：

```json
{
  "bridge_instance_id": "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
  "device_id": "bridge-a1b2c3d4e5f6"
}
```

- 手机 UDP 发现应答
- `GET /api/companion/ping`
- `POST /api/companion/pair` 的 `bridge` 对象
- `GET /api/admin/companion`

## 手机端需要修改

### 1. 配对成功后保存实例码

把 `bridge_instance_id` 和 `device_id` 与 `token`、`phone_id` 一起持久化。旧记录
没有实例码时保留为空，首次重新发现成功后补写。

### 2. 地址失败时先重新发现，不要直接判令牌失效

启动、连接失败或收到 `401` 时：

1. 执行 UDP 发现；
2. 按 `bridge_instance_id` 匹配已保存的 Bridge；旧记录没有实例码时，只有在
   名称、版本和端口唯一匹配时才自动补写；
3. 匹配成功后，把新发现的 `host + port` 写入 `baseUrl`、`lanUrl`、
   `lastSuccessfulUrl`；
4. 保留原 `token`、`phone_id`、`client_id`，用新地址重试；
5. 重试成功即恢复，不弹“令牌配对失败”。

### 3. 只有以下情况才提示重新配对

- 发现到同一 `bridge_instance_id`，但用原令牌请求仍返回 `401 unauthorized`；
- 用户主动解除配对；
- 电脑端已移除该手机。

网络地址变化、端口变化、旧 IP 不可达都不能清除令牌。

### 4. 防止连到“冒名 Bridge”

匹配必须使用 `bridge_instance_id`。仅名称相同不能视为同一台 Bridge；否则会把
令牌发给另一台同名电脑。旧记录无实例码时，匹配不唯一应让用户选择，不能自动发送
令牌。

## 验收用例

1. 已配对手机在电脑 IP 从 `.47` 改为 `.49` 后自动发现并继续使用原令牌。
2. 上述过程中不弹“令牌配对失败”，不要求重新输入配对码。
3. 电脑端口从 `8568` 改为其它端口后同样自动更新。
4. 旧 IP 被另一台同名 Bridge 占用时，不向它发送令牌。
5. 同一 Bridge 确实轮换/删除令牌后，手机才提示重新配对。
6. 旧版手机配对记录首次升级时能补写 `bridge_instance_id`，不丢原 `phone_id`。

## 复测方法

1. 手机完成一次配对并记录 `bridge_instance_id`。
2. 修改电脑 LAN IP 或切换 Wi-Fi。
3. 手机重新打开 App，执行发现。
4. 期望：发现结果实例码不变，`baseUrl` 自动更新，原 token 请求成功。
