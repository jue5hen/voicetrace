# 安全说明

声迹（VoiceTrace）是**本地单人使用**的桌面应用：Rust 壳（Tauri）负责拉起隐藏的 Python 引擎（FastAPI/uvicorn）。
本文描述 1.3.0 的本地服务威胁模型、残留风险与报告渠道。

## 威胁模型：127.0.0.1 上的本地 HTTP 服务

### 监听面
- 引擎只绑定 `127.0.0.1`，端口由壳分配（随机；`--port 0` 时由内核挑选），**不监听 `0.0.0.0`**，不对局域网暴露。
- 端口与归属写在引擎的 port 文件里（`port.json`），供壳读取。

### 身份认证
- 每次启动由壳用 CSPRNG 生成 **32 位 hex Bearer token**，只通过**环境变量 `VT_TOKEN`** 传给引擎子进程，不走命令行（命令行参数会出现在进程列表里）。
- 引擎只从 `VT_TOKEN` 或 `--token` 读取 token；两者都为空时向 stderr 打印 `engine: missing token` 并以**退出码 2** 退出——不再静默生成 uuid（那等于无鉴权）。
- HTTP 与 WS 的 token 比较使用 `hmac.compare_digest`（常数时间），不再用 `!=` 明文比较。
- 前端不硬编码端口与 token，一律通过壳的能力（`engine_info`）取。

### 令牌与凭据落盘
- `port.json` **只含 `{port, version, pid}`，不含 token**；引擎退出时（try/finally）删除，壳关闭时也会删一次，避免残留可复用凭据。
- 引擎不再把含 token 的 ready JSON 打到 stdout（壳把子进程 stdout 重定向进 `engine.log`）；日志中的 token 打码，使用说明也不再引导用户「把含 token 的日志发出来」。

### 资源访问（导出 / 缩略图）
- `/api/jobs/{id}/thumb` 校验 job_id 白名单，不再接受 `..\..\x.jpg` 这类穿越路径。
- `/api/jobs/{id}/export` 只接受数据目录内路径，或用户在文件对话框里明确选择的目标；不再对任意绝对路径自动建目录。
- 关闭 `assetProtocol`（`**` 全域 scope）与宽松 CSP：前端没有任何地方使用 asset 协议，属于纯风险零收益。

### 浏览器侧
- CORS 收紧到 Tauri 前端源（`tauri://localhost` / `http://tauri.localhost`），不再是 `*`。
- 引擎关闭 docs/openapi/access_log；前端严格 TypeScript、零 `innerHTML`/`eval`。

### 数据
- 默认数据目录是 `声迹.exe` 同级 `data/`（数据库、字幕、设置、日志、WebView 缓存）；不可写时回落到 `%LOCALAPPDATA%\VoiceTrace`；可用 `VOICETRACE_DATA_DIR` 覆盖。
- 全部推理在本机完成，不联网、不上传任何媒体或文本。
- 分发便携包时不得包含 `data/`：构建脚本每次都会强制删除（data 是运行期产物）。

### 进程
- 单实例锁：重复启动会提示已运行，避免两个实例争用 WebView2 用户数据目录（典型症状 `0x800700AA`）。
- Windows Job Object 罩住 ffmpeg 孙进程；异常退出时清理引擎与子进程，不留孤儿。

## 残留风险（本地单用户前提）

1. **同用户进程**可以读取 `data/` 下的数据库与字幕、可以读到注入的环境变量（含 token）。本程序**不防御同用户本地攻击者**，也不提供多用户隔离。
2. **无代码签名**：exe 无签名证书，SmartScreen 会拦、杀毒软件可能把便携 Python/torch 的动态库误判。分发时请核对 `SHA256SUMS.txt`。
3. `--token` 参数仅为调试保留；生产路径一律走 `VT_TOKEN`。
4. WebView2 运行时是外部依赖，其安全更新由系统负责。
5. 模型与 `.venv` 不入库：迁移目录后必须自备（见 `README.md` 前置条件），缺失时引擎直接启动失败而不是降级运行。

## 报告渠道

本项目是本地工作区项目，没有公开的 issue 跟踪器。发现安全问题时：

1. **不要公开披露细节**；
2. 直接联系维护者，附：版本号（`#source/version.txt` 或界面版本）、Windows 版本、复现步骤、影响面（能读到什么/写到什么）；
3. 日志请先脱敏：提供数据目录 `logs/` 下 `supervisor.log`、`engine.log`、`engine.prev.log`、`panic.log` 的**最后 50 行**即可；**不要**附带含 token、绝对路径或视频内容的完整日志。

修复后会在 `CHANGELOG.md` 的「安全」小节记录，并在 `docs/VERIFY.md` 增加对应的验收项。

## 支持的版本

| 版本 | 安全更新 |
| --- | --- |
| 1.3.x | ✅ 当前支持 |
| ≤ 1.2.x | ❌ 不再支持：token 明文进日志、`port.json` 含 token、路径穿越与任意写、CORS `*` 等问题未修，请升级到 1.3.0 |
