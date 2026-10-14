# Codex 手机端（CodexBridge Mobile）

> 本目录是 [CodexBridge](../README.md) 主仓库的一部分（`mobile/` 子目录），
> 桌面端伴侣接口与手机端应用随同一个仓库发布。

通过局域网连接电脑上的 **CodexBridge**，在手机上查看 Codex 的全部任务与消息、继续对话、切换模型、新建任务、处理审批请求。

- 连接方式：CodexBridge 的“手机伴侣接口”（默认端口 `8790`），同一 Wi-Fi 下自动发现（UDP `8766`）
- 令牌：配对成功后由电脑端签发，保存在手机本地，可在电脑端随时解除
- 通信：局域网明文 HTTP（仅限本地网络使用）

## 快速开始

1. 电脑端启动包含伴侣接口的 CodexBridge（`1.3.7` 或更新版本，见 CodexBridge 仓库的 `release/CodexBridge-1.3.7-win64-portable.zip`）。
2. 打开电脑端的设置页 `http://127.0.0.1:8788/settings`，在“手机连接”里：
   - 打开“启用手机连接”；
   - 记录显示的局域网地址（形如 `192.168.x.x:8790`）；
   - 点击“生成配对码”，得到 6 位数字（10 分钟内有效，仅可使用一次）。
3. 手机安装 APK（`release/CodexBridgeMobile-0.2.1-release.apk`），打开应用：
   - 点“扫描”自动发现电脑，或手动填写电脑地址（如 `192.168.1.10:8790`，不带端口时默认补 `8790`）；
   - 输入 6 位配对码，点“连接电脑”。
4. 连接成功后即可看到任务列表；点开任意任务可继续对话，右上角可切换模型。

## 功能

- 任务列表：状态、预览、模型、最近活动时间、搜索
- 对话：完整消息流（用户 / Codex / 思考 / 命令 / 文件改动 / 计划），流式输出，`Ctrl+C` 等价的“中断”
- 审批：命令执行、文件修改、权限、工具提问均可直接在手机上允许 / 本会话允许 / 拒绝 / 取消
- 新建任务：选择项目目录与模型
- 账号信息：套餐、5 小时与本周额度、token 用量
- 设置：电脑地址、版本、手机 ID、深色主题、解除配对

## 构建

前置：JDK 17、Android SDK（platform 35、build-tools 35.0.0）、Gradle 8.12。

```powershell
$env:JAVA_HOME='C:\Program Files\Eclipse Adoptium\jdk-17.0.19.10-hotspot'
gradle -p android assembleRelease
```

- 产物：`android/app/build/outputs/apk/release/app-release.apk`
- 签名：`android/signing/keystore.properties` 指向 `android/signing/codexbridge-mobile.jks`
  （别名/口令均为 `codexbridge`，仅用于个人分发与自用安装；请妥善保管，丢失后无法覆盖安装升级）

## 目录

```
android/                     Android 工程（Kotlin + Jetpack Compose）
  app/src/main/java/com/codexbridge/mobile/
    MainActivity.kt          入口与页面路由
    AppViewModel.kt          状态机（配对、任务、对话、事件流）
    data/                    伴侣接口客户端、模型、发现、设置存储
    ui/                      配对 / 任务列表 / 对话 / 设置界面
release/                     构建好的 APK
```

## 接口对照

手机端仅使用 CodexBridge 伴侣接口（`/api/companion/*`）：`ping`、`pair`、`status`、`threads`、
`threads/<id>`、`threads/<id>/messages`、`threads/<id>/interrupt`、`approvals/<id>`、`models`、
`projects`、`events`（SSE）。协议细节见 CodexBridge 仓库的 `PROTOCOL.md` 第 17 节。