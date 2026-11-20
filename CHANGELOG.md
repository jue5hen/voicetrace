# 更新记录

## 1.0.0 — 2026-11-20（首个公开版本）

- 版本号统一重置为 `1.0.0`，源代码公开到 GitHub；模型与 `.venv` 不入库。
- **新增应用内模型下载**：设置 → 离线模型，可一键下载 FireRedASR2-AED / FireRedVAD / FireRedPunc；
  支持 `hf-mirror.com` 镜像与本地代理，下载在子进程执行，可随时取消并续传。
- **新增应用内推理组件安装**：安装包不内置 CUDA 版 PyTorch（约 2.6 GB），
  设置 → 推理组件 可从 PyTorch 官方源或阿里云镜像下载并安装到运行时目录，支持 Range 续传与取消。
- 模型完整性判定收紧：`models_present` 从「目录存在」改为「推理必需文件齐备且达到最小字节数」，
  半截下载不会被误判为可用。
- 导航栏在模型缺失时显示提醒角标，首次进入工作区弹出一次提示，避免用户到首次转录才发现缺模型。

## 未发布 — 2026-11-19（减法施工，1.3.2 内，未 bump 版本）

> 施工图与逐项依据见「09修复方案（可靠性减法）」；改动前快照在
> `_archive/2026-11-19-before-subtraction/`（26 个源文件 + `frontend/mock` 整目录）。
> 方针是删死代码 / 删不划算的机制 / 改条件，不加子系统、不加依赖。

- **修 P1 · WS 空闲判死**：删掉 45s 无帧判死、`dropStale`、heartbeat 与 `online/offline` 监听。
  引擎空闲时一帧都不发，旧逻辑把「空闲」误判成「死亡」，实测每 ~60s 断连一次，
  并有一条 `refreshFromEngine → loadTranscript → transcriptError` 顶掉编辑器、丢未保存草稿的路径。
- **删契约外导出格式 `ass_karaoke`**：SPEC 未列、前端零命中；`\k` 总时长与 Dialogue 窗口不一致。
  现在请求该格式按「不支持的格式」返回 400（正确拒绝）。
- **删死设置 `vad_max_speech_ms`**：`runtime.py` 硬编码 `max_speech_frame=2000`，设置从不生效，
  却进 `_asr_signature`（sidecar 断点续跑指纹）——改它会让已完成 ASR 段全部作废。设置项 20 → 19。
- **删 `auto_retries` 字典**：与 `batch_overrides` 恒等冗余，且成功后不清零，导致降级预算「任务终身」粘滞。
  现在以 `batch_overrides` 存在性为唯一判据，成功/删除/fresh 重跑都会复位。
- **删 cue 200ms 硬底（引擎 + 前端）**：句间距 <240ms 时硬底与 40ms 间隔数学无解，实测重叠 194ms。
  现在 40ms 间隔绝对优先，只保证正时长与不重叠；顺带修掉前端末条被推过媒体时长的问题。
- **删 `build_cues` 重复 pre-trim**：与 `clamp_cue_end` 做同一件事，且会把 end 撑大，
  并把 `GAP_MS` 从 `export.py` 移除。
- **量词两表合一**：`analysis._QUANTIFIER_CLASSIFIERS` 并入 `textutil.MEASURE_CHARS`（同一对象），
  不再靠「超集测试」守；cleanup 保护面变宽（一块一块/一瓶一瓶 等）。
- **500 信封不再回显异常原文**：异常细节只进 `data/logs/engine.log`，响应固定中文文案；
  一处删除覆盖 PUT / apply / undo / 迁移全部写路径。
- **删 `frontend/mock/` 整目录**（1169 行 dev-only 第二实现）与 `dev:mock`/`mock` script；
  前端错误提示里的 `pnpm dev:mock` 文案同步改为「启动真引擎」。
- 死代码收尾：`export.MAX_CPS` 别名、`EventBridge.reconnect`、`to_ass` 的 `karaoke` 形参、
  `export._is_content` 别名、`_safe_int` 两份合一（`textutil.safe_int`）、`export` 常量改从
  `segmentation` 导入、toast 可滚动（`max-height:30vh`）。
- 测试：引擎 **234 → 233**（删 2 个 karaoke 测试、补 1 条 <240ms 密集 cue 不重叠断言）；
  前端 **52 pass** 不变；`tsc` 与 `pnpm build` 0 错。

## 未发布 — 2026-11-19（稳定性全量审计修复）

> 审计结论与逐项证据见 [`docs/AUDIT-2026-11-19.md`](docs/AUDIT-2026-11-19.md)；改动前快照在
> `_archive/2026-11-19-before-audit-fixes/`。基线测试重跑全绿后修复，修完引擎 **234**（+21 回归）、
> 前端 **52** + typecheck/build 0 错、Rust 11+6（壳未改）。

### P0

- **auto_correct 不再误删合法重叠**：单音节动词重叠（理理发/打打电话/用用心/分分钟）与「时/餐」量词重叠
  （一餐一餐/一时一时）被默认开启的自动校正静默折叠并永久写入导出。根因：`cleanup` 有保护而 `analysis`
  的 `cross_word` 分支没有，且量词超集对 时/餐 已破。保护表（`PREFIX_VERB_KEEP`/`MEASURE_CHARS`）提升到
  `textutil` 单一真源，`analysis._repeat_candidate` 补动词保护、`_QUANTIFIER_CLASSIFIERS` 保证超集。
  新增 7 条回归（含超集不变量、cleanup/analysis 判定一致）。
- **交付目录 data 卫生**：`build-release.ps1` 重构建删 `data\` 前先整目录备份到 `_archive\<日期>-release-data\`
  （robocopy 退出码 + 文件数双校验，失败即中止删除）；`run-perf.cmd` 便携冷启动改用隔离数据目录
  （`VOICETRACE_DATA_DIR`→`_scratch`），不再污染交付目录、不再删运行中实例的 port.json。
  **现有 `#release/data` 用户数据未触碰**（清理留待用户确认）。

### P1

- **重试保留旧稿可访问**：`retry_job` 不再清 `has_transcript`/`issues_json`；前端错误任务若仍有旧稿则进编辑器
  而非错误页（`Shell.tsx`/`store.tsx`）。重试再失败不会让一份在盘字幕（含用户编辑）UI 不可达。
- **edit 英文词边界**：`rebuild_words` 复用 `segmentation._inside_word`，`don't`/`well-known` 不再被切成
  `don+t`/`well+known`，词文本取原始跨度保留撇号/连字符。
- **模型白名单预检/校验**：`build-release.ps1` 预检与 `-Verify` 改按必需文件 + 最小字节数断言
  （aed/vad(VAD 子目录)/punc 逐项），半截下载不再出厂。
- **Python ABI 断言**：便携 runtime 必须 `sys.version_info[:2]==(3,11)` 且与开发 venv 一致（真实取值，非正则）。
- **-SkipPayload 不报绿**：局部重建不重写使用说明（避免与旧 SHA256SUMS 哈希不符），结尾文案改「非完整可交付包」。
- **编辑草稿切走守卫**：脏草稿时切任务/返回弹确认（`store.confirmDiscardDraft`），刷新/关窗 `beforeunload` 提示。
- **mock 对齐契约**：补 20 项设置 + 钳制 + `"false"` 解析；PUT 接受 `{settings:{…}}` 信封；sentences/apply/undo
  实现 `expected_version` CAS/409；导出绝对路径/扩展名/保留名/原子写/同名序号；CORS 白名单；405/422 信封；
  版本读真源；快照 40 版；节流 300ms。

### P2（引擎）

- lowconf 去掉 `len>=6` 门槛、`end` 用原始 text 长度（SPEC §3.3）；控制/格式字符（Cc/Cf/Cs）不计内容字；
  `export._is_content` 统一到 `textutil`；karaoke ASS 钳制到媒体时长且单条 ≤6.5s、词时间安全转换；
  cleanup/segmentation 比例回退每字 ≥1ms 禁零时长；`edit` 畸形 action（start=null）排序与 `int(None)` 词时间不再崩；
  保留名补 `CONIN$/CONOUT$`；导出捕获 `UnicodeError`；fresh 重试清 `batch_overrides`/`auto_retries`（恢复默认批大小）；
  WS `job:null` 服务端不推 + 前端守卫；sidecar 唯一临时名 + 启动清 `.tmp` 残留；probe 失败报 `probe_failed` 而非误报无音轨；
  ASR 整批空特征抛错（不再伪造空文本静默标 done）；终态 stage 与 status 一致（canceled 不再被覆盖成 done）。

### P2（前端）

- WS job 事件 null 守卫；导入 skipped 逐条列全原因；设置页 batch≤16 / window 5–120、Settings 类型与默认值补
  `gpu_memory_fraction`/`vram_headroom_mb`（reset 不丢）；队列行 Enter/Space 不再劫持内部按钮；本地 WS 去掉
  `navigator.onLine` 门禁 + 心跳 ping/静默超时自愈半开连接；浏览器回退 JSON 导出补 words；非 JSON 响应可读错误；
  ProofPanel/TaskEditor 字数统一 contentLen；JobStage 类型补 error/canceled。

### P2（脚本）

- `download_models.py` 代理 opt-in、删弃用 `resume_download`、`--repos` 默认 aed,vad,punc；`pack_smoke.py` 默认隔离
  输出目录（不覆盖用户字幕）、空 jobs 明确报错；robocopy `/XD __pycache__` 全 label 生效；构建/dev 每次
  `pnpm install --frozen-lockfile` + 退出码检查；路径守卫拒绝 8.3 短名（含 `~`）；`app` 的 `build` 改名 `build:installer`。

### 契约文档

- SPEC §3.1 stage 枚举补 `error|canceled`、`error` 明确为 `"<code>: <文案>"` 字符串；§3.3 issue id 示例改
  `suspicious:12:3:5`（与代码一致）。

### 本轮未做（需产品决策或真机/构建窗口）

- cue 重叠数学残留（<240ms）、非 BMP 索引契约、长单句切分 O(n³) 性能重构、导出同名 TOCTOU 原子预留、
  壳 shutdown 主线程阻塞、重跑覆盖编辑的确认 UX、D6 完整构建指纹/真 import torch、清单双向比对。
- 现有 `#release/data` 用户数据的清理（破坏性，待用户确认；脚本已具备备份后清理能力）。

## 未发布 — 2026-11-18（后端全量审计 · 启动回归修复）

> 全量结论、逐项证据与未修项见 [`docs/AUDIT-2026-11-18-BACKEND.md`](docs/AUDIT-2026-11-18-BACKEND.md)。

### P0 启动回归（1.3.2 引入，本次用户报告）

- **现象**：首次启动必失败，启动页「引擎进程未能启动。请查看桌面壳日志后重试」；点「重试启动」立即成功。
- **根因**：D4 把壳改为 `--port 0`、引擎自 bind 后上报，`engine_info` 在端口公布前返回 `port=0`；
  前端把 0 当启动失败直接抛错。重试时端口已公布，所以「立即成功」。另有放大器：壳的端口发现只有 5 秒，
  冷启动超时后把 0 当端口做健康检查，永远连不上，只能靠重启（缓存已热）碰巧恢复。
- **前端**：`initEngine` 在 Tauri 下轮询 `engine_info` + `engine_status` 等端口（250 ms，最长 180 s）；
  壳报 `failed/exited` 立即带根因失败；启动页显示 pid/阶段；健康检查连续失败先看壳状态；
  「重试启动」在进程已退出时先 `engine_restart`；强制重连先丢弃旧连接。
- **壳**：端口发现 5 s → 180 s，超时且无 fallback 时明确报错，不再把 0 交给健康检查；端口发现与
  健康检查共用 180 s 预算；`restart/fail/shutdown` 把端口清回 0；删除残留 port.json 前做 epoch 复核。
- **回归**：前端 6 条（端口 0→真实、failed/exited、超时、整形）；壳 5 条（stdout/port.json 优先级、
  超时 Err、早退秒失败、真实引擎 bind(0) 握手 + `/api/health`）。

### 引擎 API / 存储

- **共享字幕锁**：`transcript_lock` 在构造 Pipeline 前创建并注入，重跑终稿与编辑不再同版本互相覆盖。
- **worker 不再静默死亡**：调度/取队列/写库包进 `_run_once` + `try/except` + 1 s 退避，一次
  `sqlite3.OperationalError` 不会让所有任务永久 queued。
- **删除/编辑竞态**：`remove_job` 的 unlink 持 `transcript_lock`，并补删 `<id>.wav.meta.json` 与
  `<id>.wav.ffmpeg.log`；不会把已删任务的字幕写回。
- **设置发布**：`PUT /settings` 只 `update` 共享 dict，不再 `clear()`（转录中保存设置不会让
  `auto_export`/`keep_wav` 瞬时读到 None，也不会让并发 json 迭代 RuntimeError）。
- **导入健壮性**：probe 后 stat 失败只跳过该文件（同一批其它文件照常入队）；显式导出失败回退也走
  同名序号避让，不静默覆盖。
- **其它**：非法 action 不再 500；未知 replace scope 报错而非全篇替换；`export_dir: null` 不再变
  `"None"` 目录；`--engine-root` 生效；`/media` stat 竞态 404；相对 `export_dir` 解析为绝对路径；
  损坏 transcript 备份 `.corrupt` 并给可读错误；WS 先注册再发快照，消除窗口期丢事件；
  OOM 自动重试的 requeue 与 cancel 同锁；`edit.rebuild_words` 英文按原文分词。

### 转录核心

- **缺段即失败**：FireRedASR2 包装不再吞异常（third_party 两个 except 改为 re-raise），
  `Runtime._decode` 对缺失 uttid 抛可读错误，`_transcribe` 缺段中止任务并保留 sidecar 续跑；
  不再「done 带洞」静默丢语音，CUDA OOM 也能进入 L3 降级。
- **VAD 尾段**：28 s 固定步长留下的 <160 ms 尾段并回前片，不再丢最多 159 ms 语音。
- **版本原子**：重跑 transcript 的版本在共享锁内从磁盘现读，与编辑并发不再同版本不同内容。
- **量词不误判**：analysis 单字重复兜底排除量词头（个个人/个个都是 不再被当口吃）。
- **文件已移动**：`_run` 先检查素材存在，报 `file_missing` 而不是「没有音轨」。
- **诊断日志**：成功/失败/取消/卡死都清理 `<job>.wav.ffmpeg.log`（keep_wav 只保护 wav/meta）。
- **WS job 事件**：补 `thumb_url`/`media_url`，`job=null` 不再推送。

### 文本与导出

- **英文按词**：切分后的 words 不再拆成单字母，切点只在合法词边界；单元/片合并不再丢英文空格。
- **ASS 转义**：Dialogue 正文转义 `{ } \`，与 karaoke 一致。
- **语气词**：清理规则保护「哎呀/唉呀/咦呀/天啊/妈呀」等 keep 词。
- **口吃保守化**：单音节动词重叠（理理发/唱唱歌/游游泳/放放假/过过年/握握手/分分钟）、
  「一个一个/一条一条/一份一份」不再被删；ABAB 白名单补 30 个常用词；
  老老师/周周四/我我觉得/这个这个/好像好像 仍会清理。
- **CPS 下限**：8.5 字/秒向上取整（20 字 = 2353 ms）。
- **stats.chars 口径统一**：pipeline/segmentation/server/edit 全用 `textutil.count_chars`（内容字，
  不含标点）；前端 `contentLen` 改用 Unicode P/Z/S/C 分类，与引擎一致（SPEC §3.2）。

### 测试

- 引擎新增 `tests/test_audit_2026_11_18_{api,core,text}.py`（23 + 34 + 16 条）；全量 **213 OK**。
- 前端新增启动端口等待 6 条 + Unicode 标点 contentLen 1 条；全量 **52 OK**。
- 壳新增端口发现/真引擎握手 5 条；`cargo test` **11 lib + 6 supervisor OK**。
- 便携包重建并 `-Verify` **20/20 通过**（构建时清空运行时 `data/`，随后原样还原）。

## 未发布 — 2026-11-18（默认交互与设置页排版）

### 默认值

- **新任务默认「全部自动校正」**：`POST /api/jobs` 的 `auto_correct` 缺省值改为 `true`（引擎 `ImportBody`、`Store.add_job`、
  列默认值三处同步），前端导入也显式传 `true`。队列页「全部自动校正」因此默认勾选；仍可逐条或一键关掉。
- **转录后自动导出 SRT（默认开）**：`auto_export` 默认值由 `false` 改为 `true`。此前这个开关是**死设置**（引擎从不读取），
  本轮在 worker 里补上实现：任务成功后把 SRT 写到**视频所在目录**的 `<视频名>.srt`。

### 自动导出行为

- 与手动导出共用同一套落盘规则：临时文件 + `os.replace`、同名自动追加 ` (2)`/` (3)`、拒绝 Windows 保留设备名，
  **不覆盖**已有字幕文件。
- 视频目录不可写（只读盘、权限不足、素材已被移走）时回退 `data/exports` 并推 `warn` toast；
  导出失败只记日志与提示，**不会**把已经成功的任务改成 `error`。
- 手动导出仍使用设置里的「默认导出目录」（`export_dir`），与自动导出的目标互不影响。

### 设置页

- **改为纵向单列排版**：分区自上而下逐行排列，每个设置项「标签/说明在上、控件在下」；移除原两列 `settings-pair` 包裹，
  页头状态行也改为标题下方一行。列宽收到 920px，长说明更易读。

### 测试

- 新增引擎回归：导入默认 `auto_correct=true`（显式 `false` 仍生效）、自动导出落在视频目录、同名不覆盖、关闭时零写入；
  `test_store` 的默认值断言同步更新。

## 未发布 — 2026-11-18（补充审计修复）

> 依据桌面文档《声迹09_补充审计与详细修复方案_2026-11-17.md》。逐条复核后确认 A1–A11、B2–B9、C8–C10、D3–D5 属实并修复；
> B1 采用审计给出的短期方案（共享锁 + 唯一临时文件），主存迁移保留为产品决策；D1（Job 原子绑定）、D6（构建完整性）
> 需要真机/构建窗口，未在本轮实施。回归与逐项结论见 `docs/AUDIT-2026-11-18-FIXES.md`。

### 数据与字幕（P1）

- **A1 迁移覆盖人工编辑**：`load_transcript` 的「读盘 → 判断 seg_version → 迁移 → 落盘」整段放进 `transcript_lock`，
  锁内重新读盘；迁移本身也递增 `version`，不再用锁外旧快照把 v2 回退成 v1。
- **A2 截断音轨被复用**：WAV 缓存新增 `<id>.wav.meta.json` 指纹（时长 + 字节数），命中时还要用 soundfile 实测时长
  （±max(2s, 0.1%)）；不符即重抽，抽完再校验一次。
- **A3 单行字幕丢字**：`wrap_subtitle(max_lines=1)` 改为按内容字符预算裁剪（与切句口径一致），标点转空格不再吃掉末字。
- **A4 少字 + 长停顿崩溃**：`_split_piece` 的 `k` 钳到字符数 `n`，每片至少 1 字，不再 `IndexError`。
- **A5 ffmpeg 进度线程 NameError**：实现 `_parse_ts`（HH:MM:SS.mmm → 秒）并给 `_reader` 加逐行异常兜底，读线程不再静默死亡。
- **A6 >5h 跨窗漏语音**：窗口 VAD 段按 `[core_start, core_end]` 裁剪后归属唯一窗口，相邻半段由 `_normalize_segments`
  合并；不再整段丢弃跨核心起点的语音。
- **A7 构建目录绕过**：`-ReleaseDir` 先用 `[IO.Path]::GetFullPath` 规范化再比较（正斜线 / `..` / 大小写），
  并禁止 release 是 `#source` 的祖先或后代。
- **A8 撤销先删历史**：改为「peek 栈顶 → 保存成功 → 按 id 删除」；写失败可重试，不再首次 500、重试 409。
- **A9 设置写失败清空内存**：先 `save_settings`（唯一临时文件 + fsync + `os.replace` + 文件锁）成功后再替换
  `state.settings`；失败保留完整旧配置。
- **A10 句中插字零时长**：`_fill_gaps` 改为对后续保留字做时间平移（而不是压平），保证 `end > start`。
- **A11 末条越过媒体结尾**：`media_end` 作为硬上限优先于 200ms 硬底；`start >= media_end` 的退化 cue 丢弃。

### 生命周期与一致性（P1/P2）

- **B2 版本单调**：重跑生成的 transcript 读取旧稿版本再 +1；迁移也 +1；前端写请求携带 `expected_version`（见 C3）。
- **B3 取消边界**：worker 认领任务改为在 `lifecycle_lock` 内重读状态；最终提交与取消在同一把锁内裁决；
  OOM 自动重试先检查 cancel event / 取消集合，用户取消优先。
- **B4 删除 / 重试生命周期**：删除先打 tombstone → 取消 → 等 worker 退出再清文件，启动时补扫孤儿 sidecar；`processing` 中 retry 返回 409；
  `fresh` 清理 sidecar 失败不再谎报成功。
- **B5 续跑指纹**：sidecar 升到 v2，记录每段 `start_ms/end_ms` + 媒体/引擎/ASR 参数指纹；逐段校验边界后才复用。
- **B6 首次 DB 写失败**：认领更新移入 try，失败也会走 finally 释放 cancel event；worker 检测到行仍 queued 时退避 1s，
  不再紧循环。
- **B7 模型半加载 / CPU 设置**：`settings.device=cpu` 时即使 CUDA 可用也用 CPU；加载失败清空 vad/punc/asr 引用并
  `empty_cache`，重试从干净状态开始。
- **B8 sidecar 写失败**：未落盘结果保留在内存，进程内 OOM 重试直接复用；显式 warn toast，不再宣称「已保存」。
- **B9 关闭竞态**：worker 5 秒未退出再等 25 秒；仍未退出则跳过 checkpoint/close，避免「closed database」。
- **B1（短期）**：`pipeline._save_transcript` 与编辑路径共用 `transcript_lock` + 唯一 `.json.tmp`。

### 服务层与前端契约（P2）

- **C4 撤销深度**：新增 `GET /api/jobs/{id}/history -> {count}`；前端据此驱动 undo，不再每次加载清零。
- **C8 同名导出覆盖**：默认导出名冲突时自动追加 ` (2)`、` (3)`；显式 `path` 仍视为用户确认覆盖。
- **C9 同批重复导入**：循环内把新任务路径加入去重集合，同一路径两次只建一个任务。
- **C10 CORS / Range**：CORS 收紧到 Tauri 源 + 本地 Vite 源；`bytes=-N` 正确解析为末尾 N 字节。
- **C1–C7**：保存中草稿保留、失败态不称已保存、重连过期回包丢弃 + CAS、撤销深度、Ctrl+Enter、末条结束后隐藏、
  队列「已取消」分组（详见前端提交）。

### 桌面壳与构建（P2）

- **D3 ready 后看护**：新增 watchdog，每 2 秒 `try_wait`；崩溃转 failed/exited 并关闭 Job 带走孙进程。
- **D4 端口契约**：壳不再预选端口，统一传 `--port 0`，由引擎 bind(0) 后经 stdout/port.json 上报；重启同样重新取端口。
- **D5 早退诊断**：`status_report.log_tail` 优先读 `engine.out.log`（ImportError 等初始化错误在这里）。
- **D2 生命周期锁**：shutdown/restart/adopt/fail 共用 `restart_lock`，shutdown 为不可逆终态；状态写入做 epoch 校验；
  `stop_child` 先关 Job，等待进程退出限时 5 秒。
- **D7 构建临时目录**：前端构建镜像从 `%TEMP%` 移到项目 `_scratch/<日期>/`。


## 更早版本（已归档，仅摘要）

- 1.3.2（2026-11-17）：头部余量语义修正——`vram_headroom_mb` 是下限不是加数，避免长视频级联缩到 batch=1。
- 1.3.1（2026-11-17）：批量长视频四层显存防御（L0 上限 / L1 自愈 / L2 断点续跑 / L3 无人值守降级）。
- 1.3.0（2026-11-17）：工程化地基（切分守恒、单实例、Job Object、CSP 与出站收紧）。

> 1.3.0 之前的记录已回收；旧细节用 `git log` / 旧版本文档查。
