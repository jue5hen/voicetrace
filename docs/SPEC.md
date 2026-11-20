# 声迹 VoiceTrace — 产品与技术规格（唯一契约）

> 版本 **1.0.0** · 最后更新 2026-11-20（对应源码 1.0.0；历史审计与缺陷清单见 `AUDIT-2026-11-19.md`）
> 本文件是唯一契约：改协议、改数据形状、改安全模型，先改这里。

## 1. 目录与归属

`
<项目根>\
  README.md                     # 项目说明（前置条件/构建/排障）
  CHANGELOG.md                  # 版本变更
  SECURITY.md                   # 本地服务威胁模型
  docs\SPEC.md                  # 本文件，契约
  docs\VERIFY.md                # 当前版本验收清单
  #source\version.txt           # 版本唯一真源（单行，例如 1.3.0）
  #source\engine\               # Python 引擎：voicetrace_engine\ + tests\
  #source\frontend\             # React 18 + Vite + TS
  #source\app\                  # Tauri 2 Rust 壳
  #source\scripts\              # build-release.ps1 / dev.ps1 / make-icon.py
  #release\声迹_portable\        # 最终便携目录
  _archive\                     # 退役物与改动前快照
`

## 2. 运行时形态

- 便携目录（最终交付）：
  `
  声迹.exe
  engine\voicetrace_engine\...      # 纯 .py
  engine\third_party\FireRedASR2S\  # 上游源码
  models\{aed,vad,punc}
  runtime\python\python.exe         # 可整目录拷贝的 CPython 3.11
  runtime\site-packages\...         # torch 2.1.0+cu118 等
  runtime\ffmpeg.exe, ffprobe.exe
  data\                             # 运行期生成，绝不进包
  `
- 壳启动引擎的方式：
  `
  runtime\python\python.exe -m voicetrace_engine
    --port 0 --data-dir <dir>\data --models-dir <dir>\models --ffmpeg <dir>\runtime
    --parent-pid <壳 pid>
    （环境变量）VT_TOKEN=<32 位 hex>、PYTHONPATH=<dir>\runtime\site-packages
  `
- **token 只经环境变量传递**，不出现在命令行（进程列表可见）。引擎若既没有 `--token` 也没有 `VT_TOKEN`，
  必须向 stderr 打印 `engine: missing token` 并以退出码 2 结束——**不允许静默生成随机 token**。
- **`port.json`**（`--port-file` 指定）只含 `{port, version, pid}`，**不含 token**；引擎退出时删除它，
  壳在关闭时也删一次。
- 数据目录：默认 `声迹.exe` 同级 `data\`（库、字幕、日志、WebView 缓存）；不可写时回落 `LOCALAPPDATA%\VoiceTrace`。
- 引擎只监听 `127.0.0.1`；端口由引擎自行 `bind(0)` 后上报，避免「先探测空闲端口再占用」的竞态。
- 开发模式：`python -m voicetrace_engine --port 8765 --token dev`（token 可显式给，PATH 兜底 ffmpeg）。

## 3. 引擎 HTTP API（base = `http://127.0.0.1:{port}/api`）

除 `/health` 外全部要求 `Authorization: Bearer <token>`；媒体/缩略图/WS 允许 `?token=`。
错误统一 `{"error": {"code": "...", "message": "..."}}`，HTTP 4xx/5xx；401/404/405/422/500 都必须走这个信封。
500 的 `message` 是固定中文文案，异常细节只进 `data/logs/engine.log`，**不回显给客户端**。
token 比较使用常量时间比较（`hmac.compare_digest`）。CORS 只允许 Tauri 源与本地 Vite 开发源。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /health | `{ok, version, uptime_s}`，免鉴权 |
| GET | /system | 引擎/模型/队列/路径状态（见 §3.6） |
| GET | /models | 离线模型下载状态（见 §3.7） |
| POST | /models/download | `{repos?, proxy?, endpoint?}` 启动缺失模型下载，立即返回状态 |
| POST | /models/cancel | 终止下载子进程，返回当前状态 |
| GET | /runtime | 推理组件（CUDA PyTorch）安装状态（见 §3.8） |
| POST | /runtime/install | `{proxy?, mirror?}` 下载并安装运行时，立即返回状态 |
| POST | /runtime/cancel | 终止安装子进程，返回当前状态 |
| GET/PUT | /settings | 用户设置 |
| POST | /jobs | `{paths:[abs,...], auto_correct?}`（缺省 `true`）→ `{jobs:[Job], skipped:[{path, reason}]}` |
| GET | /jobs | `{jobs:[Job]}`，按创建时间倒序 |
| GET | /jobs/{id} | Job |
| DELETE | /jobs/{id} | 删除任务与结果 |
| POST | /jobs/{id}/retry | 重新排队 |
| POST | /jobs/{id}/cancel | 取消（排队或进行中），结局是 `status=canceled` |
| POST | /queue | `{paused:bool}` |
| GET | /jobs/{id}/transcript | Transcript |
| GET | /jobs/{id}/history | `{count}`：服务端撤销历史深度（前端据此启用 undo） |
| PUT | /jobs/{id}/sentences/{sid} | `{text, expected_version?}` 整句改写 → `{transcript, issues}` |
| POST | /jobs/{id}/apply | `{actions:[Action]}` → `{transcript, issues}` |
| POST | /jobs/{id}/undo | → `{transcript, issues, undone}` |
| GET | /jobs/{id}/issues | `{issues:[Issue], summary:{suspicious,repeat,lowconf,total}}` |
| POST | /jobs/{id}/export | `{format, path?}` → `{path}` |
| GET | /jobs/{id}/media | Range 流式视频 |
| GET | /jobs/{id}/thumb | JPEG 缩略图；**job 不存在一律 404**（不得用未校验的 id 拼路径） |
| WS | /ws?token= | 事件推送（见 §3.6） |

- **并发写**：`PUT /sentences`、`POST /apply`、`POST /undo` 三条读改写路径共用同一把锁，互不覆盖；
  请求体带 `expected_version` 时做 CAS，不带则跳过（向后兼容）。
- **导出路径**：`path` 必须是绝对路径、扩展名必须与 `format` 匹配、拒绝 Windows 保留设备名；
  写入走「临时文件 + `os.replace`」。不传 `path` 时用 `export_dir` 或 `data\exports\`；
  同名默认导出自动追加 ` (2)`、` (3)` 序号，**不得静默覆盖**已有文件（显式传 `path` 视为用户确认覆盖）。
- **转录后自动导出**（`auto_export`，默认开）：任务成功后由 worker 把 SRT 写到**视频所在目录**的
  `<视频名>.srt`，与手动导出共用同一套原子写 / 同名追加序号 / 保留设备名规则；视频目录不可写
  （只读、权限、素材已被移走）时回退 `data\exports\` 并推 `warn` toast；导出失败只记日志与提示，
  **不得**把已成功的任务改成 error。
- **迁移与版本**：旧稿迁移、编辑、apply、undo、重跑都只增不减 `version`；迁移在
  `transcript_lock` 内重新读盘再判断，禁止用锁外快照覆盖并发保存。

### 3.1 Job

```
{"id":"8f3c...","name":"第01讲.mp4","path":"D:\\video\\第01讲.mp4",
 "size_bytes":812345678,"duration_ms":4512000,
 "status":"queued|processing|done|error|canceled",
 "stage":"queued|load|probe|extract|vad|asr|punc|finalize|done|error|canceled",
 "progress":0.42,"error":null,
 "created_at":"2026-11-02T23:00:00+08:00","started_at":null,"finished_at":null,
 "rtf":0.18,"width":1920,"height":1080,"has_transcript":false,"issues_count":0,
 "thumb_url":"/api/jobs/8f3c/thumb","media_url":"/api/jobs/8f3c/media"}
```

- `stage=load` 表示该任务触发了首次模型加载（20–90 秒）；它必须在 `pipeline` 真正加载模型前发出。
- 无音轨的文件不进入识别：直接 `status=error`，`error` 为字符串 `"no_audio: 该文件没有音轨，无法转录"`
  （`error` 字段统一是 `"<code>: <中文文案>"` 字符串，不是对象；probe 本身失败报 `probe_failed`）。
- `auto_correct`（Job 字段）：新任务默认 `true`（「全部自动校正」默认勾选）；`POST /jobs` 显式传
  `auto_correct:false` 或 `POST /jobs/{id}/auto-correct {enabled:false}` 才关闭。

### 3.2 Transcript

```
{"job_id":"8f3c","version":3,
 "media":{"name":"第01讲.mp4","path":"D:\\...","duration_ms":4512000,"width":1920,"height":1080},
 "sentences":[
   {"id":0,"start_ms":1200,"end_ms":5600,"text":"大家好，欢迎来到今天的课程。","conf":0.93,
    "words":[{"text":"大","start_ms":1200,"end_ms":1380}, ...],"edited":false}
 ],
 "stats":{"chars":12345,"sentences":560,"duration_ms":4512000,"rtf":0.18,"elapsed_s":812}}
```

- `words` 不含标点，按时间升序；中文通常一字一词，**英文按单词**（不得拆成单字母）。
- `text` 含标点；Issue 的 `start/end` 是 **text 的字符下标**（左闭右开，BMP 内 Python 字符索引 = JS UTF-16 索引）。
- 切句后**内容必须守恒**：各句内容字符合起来等于原句，且每句 ≤ `subtitle_max_chars` 有效字、≤6.5 秒。
- `stats.chars` 与前端 `contentLen` 口径一致（内容字符，不含标点）。
- 导出 cue 时间轴：40 ms 条间间隔**绝对优先**；密集语流允许本条短于 900 ms（最短 1 ms），
  硬不变量是**不重叠 + 正时长**；末条不越过媒体时长。

### 3.3 Issue

```
{"id":"suspicious:12:3:5","kind":"suspicious|repeat|lowconf","sentence_id":12,
 "start":3,"end":5,"text":"圣兵","suggestion":"胜兵",
 "count":10,"variant_count":1,"score":0.9,"reason":"同音异写 · shèng bīng","resolved":false}
```

- `suspicious`：同/近音 n-gram 频次对比，`suggestion` 为高频写法。
- `repeat`：口吃重复，`start/end` 覆盖重复段（含两份），`suggestion` 为去重后的文本。
- `lowconf`：句级置信度 < 0.55（整句，start=0,end=len），`suggestion` 为空。
- id 统一格式 `<kind>:<sentence_id>:<start>:<end>`。

### 3.4 Action（/apply）

```
{"type":"replace","from":"圣兵","to":"胜兵","scope":"all","sentence_ids":[]}
{"type":"replace","from":"圣兵","to":"胜兵","scope":"selected","sentence_ids":[12,19]}
{"type":"repeat","sentence_id":12,"start":0,"end":4}
{"type":"dismiss","issue_id":"s:12:3:5"}
```

返回应用后的完整 transcript 与重算后的 issues。服务端每次 apply 前写快照（保留 40 版），`/undo` 恢复上一版。

### 3.5 Settings（默认值，19 项）

```
{"device":"auto","half":true,"beam_size":3,"batch_size":6,
 "window_minutes":30,"min_ngram_count":3,"fuzzy_pinyin":false,
 "subtitle_max_chars":22,"subtitle_max_lines":2,
 "subtitle_clean_fillers":true,"subtitle_clean_stutter":true,
 "subtitle_merge_gap_ms":900,"subtitle_punct_space":true,
 "keep_wav":false,"auto_export":true,"export_dir":"","reduce_motion":false,
 "gpu_memory_fraction":0.88,"vram_headroom_mb":700}
```

- `gpu_memory_fraction`（0.5–0.95）：本进程显存硬上限（整卡比例）。`vram_headroom_mb`（200–4000）：每批前要求的最小显存余量
  ——**作为下限**：缩批条件是 `空闲 < max(需求×1.3, vram_headroom_mb)`，不要改成加法（平台期会级联缩到单段）。
  两项配合 `batch_size` 构成「别撞显存」的第一层。

- 写入 `data\settings.json`（临时文件 + `os.replace` 原子写）；损坏时回退默认并备份为 `settings.json.corrupt`。
- 数值项有上下限（`beam_size` 1–8、`batch_size` 1–16、`window_minutes` 5–120、`subtitle_max_chars` 10–48 等），越界钳制。
- 字符串 `"false"` 解析为假（不要用 `bool("false")` 的语义）。

### 3.6 /system 与 WS 事件

`/api/system` 必含：`version`、`device`、`model_state`（`idle|loading|ready|failed`）、
`models_present{aed,vad,punc}`（布尔，**必需文件齐备**，不是目录存在性）、`sample_rate`(16000)、`queue`、`paths`、
`models_download`（下载状态，见 §3.7）。

WS 事件：
- 连接建立后**立即**推 `{"type":"snapshot","jobs":[...],"queue":{...},"engine":{...}}`
- `{"type":"job","job":{...}}`（300 ms 节流）、`{"type":"queue","queue":{...}}`、
  `{"type":"engine","engine":{...}}`、`{"type":"toast","level":"info|warn|error","message":"..."}`
- 广播失败不得静默吞异常；断开连接要从广播集合移除。

### 3.7 离线模型下载

模型权重来自 Hugging Face 的 FireRedASR2S 公开仓库（`FireRedTeam/FireRedASR2-AED`、`FireRedVAD`、`FireRedPunc`），
由引擎在子进程中下载，绝不由前端直接访问外网。

- `GET /models` 与 `/system.models_download` 返回同一形状：
  `{state, current, progress, downloaded_bytes, total_bytes, error, repos, models:{aed,vad,punc:{label,ready,bytes,total}}}`；
  `state` ∈ `idle|running|done|error|canceled`。
- `POST /models/download` 只下载当前缺失的模型；已有完整文件的模型跳过。`proxy`/`endpoint` 必须是 `http(s)://` 开头。
  `endpoint` 用于切换镜像（如 `https://hf-mirror.com`）。
- 下载在独立子进程中执行，`POST /models/cancel` 直接终止子进程；未完成的 `.incomplete` 文件保留，下次继续。
- **完整性判据**：只认推理实际加载的文件与最小字节数（与 `scripts/build-release.ps1` 的模型白名单一致），
  半截文件永远不算就绪。
- 下载失败只影响模型文件，不改变 `model_state`；错误详情写入 `data/logs/model-download.log` 并在状态中回传尾部。

### 3.8 推理组件（CUDA PyTorch）

安装包**不内置** torch：CUDA 版解压后约 4.9 GB，NSIS 安装器与 GitHub Release 单文件上限都是 2 GB。
首次使用由应用内下载官方 wheel（torch 2.1.0+cu118 + torchaudio，共约 2.6 GB），再安装到 `runtime/site-packages`。

- `GET /runtime`（与 `/system.runtime_setup` 同形状）：
  `{state, phase, torch_ready, downloaded_bytes, total_bytes, progress, error, supported}`；
  `state` ∈ `idle|running|done|error|canceled`，`phase` ∈ `download|install|done`。
- `POST /runtime/install`：`proxy` 必须是 `http(s)://`；`mirror=true` 优先使用阿里云镜像。
  wheel 下载到 `runtime/.downloads`，支持 HTTP Range 续传；已完整下载的 wheel 会跳过。
- 安装通过子进程执行 `pip install --no-deps --no-index --target runtime/site-packages`，
  **不访问 PyPI**，也不改动用户其它 Python 环境。
- `POST /runtime/cancel` 直接终止子进程；未完成的 `.part` 文件保留，下次续传。
- 安装完成后 `torch_ready=true`；CUDA 上下文在引擎进程内初始化，建议重启引擎后再转录。

## 4. 前端（React 18 + TS + Vite，无 UI 框架）

设计语言：浅色、克制、森林绿强调。**后续统一到 `_tools/design/design-tokens.css`（与 03–06 同一套 token）**。

```
--bg:#f3f8f2 --panel:#fff --panel-2:#f7fbf6 --line:#d7e5d3
--text:#19352a --muted:#5f7469 --faint:#8aa294
--accent:#1f8a5b --accent-ink:#fff --ok:#1f8a5b --warn:#b7791f --err:#c0392b
radius 12/16/20, shadow 0 18px 48px rgba(25,53,42,.08)
字体：Segoe UI, "Microsoft YaHei UI", "PingFang SC", system-ui
```

布局（单页，无路由库）：
1. **启动页**：Logo + 引擎状态。只等 `/health` 与 `model_state`；文案必须说明「模型按需加载，首次转录 20–90 秒」。
   `model_state=failed` 或引擎早退时，调用壳的 `engine_status` 展示 `exit_code`/`log_tail`，提供「重启引擎」（`engine_restart`）。
2. **主界面**：左侧 208px 导航（转录 / 批量转录 / 设置 / 引擎状态），主区按视图切换。
3. **空状态**：整块可点拖放区 + 三步指引；页头只保留 `Ctrl+O` 提示。
4. **任务编辑页**：顶部文件信息条；左侧媒体预览铺满（视频按比例、音频声波、叠加当前句）；右侧字幕工作面板。
5. **右侧面板**（常驻，默认「字幕」，不可折叠）：字幕 / 校对建议两标签；字幕区虚拟滚动、行内高亮；
   校对区按 疑惑词 / 重复词 / 低置信 分组，支持勾选与批量处理。
6. **底部操作条**：撤销、保存状态、导出（srt/vtt/ass/txt/json）。
7. **设置页**（独立页面，`Ctrl+,`）：**纵向单列**四组（每项「标签在上、控件在下」）+ 底部固定保存栏，写入 `data\settings.json`。

交互要求：
- 点击字幕行 → seek；空格播放/暂停；↑↓ 选行；Enter 编辑；`Ctrl+Z` 撤销；`Ctrl+E` 导出；`Ctrl+,` 设置；`Ctrl+O` 打开。
- 编辑句子：双击行展开编辑条，Esc 取消，`Ctrl+Enter` 保存。
- 拖放导入（Tauri 拖放事件或 `input[type=file]` 回退）。**导入被跳过的文件必须逐条提示原因**。
- 队列进度实时（WS）；**断线重连后必须补齐 jobs、当前 transcript 与 issues**，不能停在旧状态。
- **跨任务竞态**：任何异步写结果返回时，若发起时的 `jobId` 已不是当前任务，必须丢弃结果（不得写入当前面板）。
- 无障碍：正文对比度 ≥4.5:1、控件边界 ≥3:1、可见焦点环、模态有焦点陷阱。

引擎连接（`src/lib/engine.ts`）：
```ts
// Tauri: invoke("engine_info") -> {port, token}
// 浏览器: VITE_API_BASE(默认 http://127.0.0.1:8765) + VITE_TOKEN(默认 dev)
```

- **端口未公布不是启动失败**：引擎自 `bind(0)` 后上报端口，壳在拿到端口前
  `engine_info.port = 0`。前端必须轮询 `engine_info`（端口出现）与 `engine_status`
  （phase / pid / error），最长 180 秒；只有壳明确报 `failed`/`exited` 时才进入失败页，
  超时才提示超时。**禁止把 `port=0` 当成「引擎进程未能启动」直接报错**。
- 壳重启引擎时先把 `port` 清回 0，前端重试会重新等新端口，不会连旧端口空转。

开发：`pnpm dev` 连真引擎（端口 8765，token dev）。mock 已删除：UI 调试一律连真引擎。

## 5. Tauri 壳（Rust）

- 包名 `voicetrace`，identifier `com.voicetrace.desktop`，窗口 **1600×900（min 1080×720）**，
  无边框自绘标题栏，关闭即退出并杀引擎；**只记忆位置、不记忆尺寸**。
- 单实例：命名互斥体（名字含数据目录规范化路径）；已存在实例时提示并前置，不启动第二个引擎。
- 子进程治理：`CREATE_NO_WINDOW`；**Job Object（KILL_ON_JOB_CLOSE）** 罩住子进程树，
  壳崩溃时 python 与 ffmpeg 孙进程必须一起结束。
- 启动：生成 32 位 CSPRNG token → `VT_TOKEN` 传入 → 引擎自 `bind(0)` 后经 stdout/port.json
  上报端口（与健康检查共用 180 秒启动预算；进程早退由 `cancelled()` 秒级失败）→ 轮询 `/api/health` →
  `engine_info` 返回 `{port, token}`；端口公布前 `port=0`，失败/退出/重启时清回 0。
  失败时把 `exit_code` 与日志尾巴交给前端。
- 命令：`engine_info`、`engine_status`、`engine_restart`、`open_path`。
- 安全：`csp` 必须配置（生产策略；开发用 `devCsp: null` 以免干扰 Vite HMR）；
  **不得启用 assetProtocol**（前端不使用），Cargo 不启用 `protocol-asset`。
- 日志：`data\logs\{supervisor,engine,engine.prev,panic}.log`；engine.log 按 5 MB × 3 轮转，**不得写入 token**。

## 6. 构建

- 前端：`pnpm install --frozen-lockfile && pnpm build`（tsc --noEmit 零错误）→ `#source/frontend/dist`。
- 壳：`cargo build --release --features custom-protocol`（**缺这个 feature 会白屏**）。
- 打包：`pwsh -NoProfile -File #source\scripts\build-release.ps1`
  - 版本：`-BumpVersion <x.y.z>` 一次改五处（`version.txt`、`Cargo.toml`、`tauri.conf.json`、两个 `package.json`）；
    每次构建与 ` -Verify` 都断言五处一致。
  - 预检（ffmpeg/python/models/site-packages）在拷贝**之前**；拷贝前清空目标端 `engine/ models/ runtime/ data/`。
  - robocopy 退出码 ≥8 一律失败；`-Skip*` 全给时必须明确「未产出可交付包」并非 0 退出。
  - 产出 `SHA256SUMS.txt`；`-Verify` 逐项校验（exe 版本、模型、runtime 可执行、无 `tf_model.h5`、版本一致）。
  - `使用说明.txt` 写版本号、四份日志路径、数据目录与回落规则、常见故障。
- 验收：双击 `声迹.exe` → 拖入视频 → 转录完成 → 校对 → 导出 SRT。

## 7. 质量门槛

- 后端单测：`python -B -m unittest discover -s tests` 全绿；**必须包含** API 契约测试（鉴权/信封/穿越/导出/并发 CAS/WS 快照）、
  切句守恒与 k≥3 回归、cue 不重叠且不越界、编辑插入零时长回归、ffmpeg stderr 死锁回归、
  **显存自适应（缩批 / OOM 折半 / `AsrOutOfMemory` / 回调隔离）与断点续跑（sidecar 合并 / 指纹不符丢弃 / 自动降级）回归**。
- 壳：`cargo test` 全绿（含 supervisor 用例）。
- 前端：`pnpm typecheck` 与 `pnpm build` 零错误、`pnpm test` 全绿；
  必须包含 cue 不变量（相邻不重叠、正时长、≤6.5 秒、末条不越过媒体时长）与导出形状断言；
  关键交互键盘可用；对比度达标。
- 打包：`build-release.ps1 -Verify` 全绿 + `SHA256SUMS.txt` 校验通过。
- 真实视频（≥20 分钟）端到端跑通，RTF 记录在 README。
- 全程离线可用（模型、字体、图标本地）。
