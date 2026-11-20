# 声迹 VoiceTrace · 验收清单（v1.3.2）

> 验收日期：2026-11-17 · 产物：`#release/声迹_portable`（20,847 个文件 / 10.47 GB）
> 依据：`docs/SPEC.md`（唯一契约）· 施工依据：`docs/AUDIT-2026-11-19.md` · 变更：`CHANGELOG.md`

## 0. 一分钟跑通

1. 打开 `#release/声迹_portable/`，双击 **声迹.exe**。
2. 启动页显示引擎状态；模型**按需加载**，首次转录前会显示「加载模型」，约 20–90 秒。
3. 把任意带声音的视频拖进窗口（或按 `Ctrl+O`）。
4. 任务卡片显示 `加载模型 → VAD → 识别 → 标点` 进度，完成后自动打开字幕列表。
5. 右侧面板「字幕」标签：当前句按导出折行预览，列表可单击定位、双击编辑；「校对建议」列出 疑惑词 / 重复词 / 低置信。
6. 点任意提示 → 播放器跳到对应句子；「应用」→ 文本与时间轴即时更新；`Ctrl+Z` 可撤销。
7. 右上「导出」→ 选 SRT → 默认存到视频同目录同名 `.srt`。

## 1. 功能核对

| # | 需求 | 验收动作 | 期望 |
| --- | --- | --- | --- |
| 1 | 视频转录 | 拖入 mp4/mkv | 自动抽音频、VAD、识别、分句，出词级时间轴 |
| 2 | 长视频 | 拖入 ≥30 分钟视频 | 进度连续、内存稳定；1 小时素材 RTF 记录在 README |
| 3 | 批量逐个 | 一次拖入 3 个视频 | 队列依次处理，同一时刻只跑一个，可暂停/继续/取消 |
| 4 | 词级时间轴 | 导出 JSON | `words[]` 含每个词 start/end（karaoke ASS 已删，见 CHANGELOG） |
| 5 | 疑惑词 | 找一条「疑惑词」 | 含正确写法、错误写法、次数、同音拼音；可单条应用 |
| 6 | 一键全篇替换 | 点某条「全部替换」 | 全篇同一异写一次替换，字幕与建议数同步刷新 |
| 7 | 重复词 | 找一条「重复用词」 | 显示「好像好像 → 好像」或「老老师 → 老师」，可一键应用 |
| 8 | 合法叠词 | 查看「说说/看看/爸爸/高高兴兴」 | 不应出现在重复提示里，也不被清理 |
| 9 | 口吃与语气词清理 | 导入含「我我觉得」「这个这个」「嗯，那个」的素材 | 字幕直接是「我觉得」「这个」「那个」；时间轴连续；设置可关 |
| 10 | 撤销 | 应用后 `Ctrl+Z` | 文本、时间轴、提示列表回到上一步（并发保存不再产生不存在的中间态） |
| 11 | 导出 | 分别导出 SRT/VTT/ASS/TXT/JSON | 播放器可读；ASS 含样式；**相邻 cue 不重叠、末条不超过视频时长** |
| 12 | 隐藏后端 | 任务管理器 | 只有 声迹.exe 与一个 python.exe，**无控制台窗口**；只监听 127.0.0.1 随机端口 |
| 13 | 隐私 | 断网后转录 | 全流程可用，无外网请求 |
| 14 | **无音轨文件**（1.3.0 新增） | 导入纯视频/损坏文件 | 直接报错「该文件没有音轨，无法转录」，不再永远卡在「识别中」 |
| 15 | **重复启动**（1.3.0 新增） | 连点两次 exe | 第二个实例提示「已在运行」并前置已有窗口，不再因 WebView2 数据目录被占而闪退 |
| 16 | **引擎失败可见**（1.3.0 新增） | 临时改坏 models 路径 | 启动页显示退出码与日志尾巴，提供「重启引擎」 |

## 2. 界面核对

- 浅色主题（#f3f8f2 底 / #1f8a5b 强调），自绘标题栏可拖动、最小化/最大化/关闭；窗口 1600×900（最小 1080×720）。
- 左侧 208px 导航：工作区（转录 / 批量转录）、设置、引擎状态；首页只有一个导入入口（整块拖放区，`Ctrl+O` 等价）。
- 编辑器两栏：左侧媒体铺满（视频按比例、音频声波、叠加当前句），右侧字幕面板（「字幕 / 校对建议」，不可折叠）。
- 空状态有明确引导；队列页「队列空闲」可直接点开导入；导入被跳过的文件会逐条提示原因。
- 处理中显示阶段（含「加载模型」）、百分比、预计剩余；失败显示错误与重试。
- 设置是独立页面（`Ctrl+,`），四组 + 底部固定保存栏，写入 `data/settings.json`，重启保留。
- 键盘：空格播放/暂停，↑↓ 切句，回车编辑，`Ctrl+Z` 撤销，`Ctrl+E` 导出，`Ctrl+,` 设置，`Ctrl+O` 打开。
- 无障碍：正文对比度 ≥4.5:1、控件边界 ≥3:1、可见焦点环（2px+2px）、模态有焦点陷阱、确认框 Enter 只走默认按钮。

## 3. 已自动化验证（2026-11-17 实跑）

| 项 | 结果 |
| --- | --- |
| 引擎 unittest（`python -B -m unittest discover -s tests`） | **84/84 通过**（6.97 s；含 API 契约、切句守恒、cue 不重叠、编辑零时长、ffmpeg 死锁回归） |
| Tauri `cargo test --offline` | **12/12 通过**（6 库单测 + 6 supervisor） |
| 前端 `pnpm typecheck` | 0 错误 |
| 前端 `pnpm build` | 0 错误，2.9 s；主 JS 252.64 kB（gzip 82.94）+ CSS 93.06 kB（gzip 18.20） |
| 前端单测（`pnpm test`，node:test） | **32/32 通过**（0.3 s）：cue 不重叠/正时长/≤6.5s/末条不越界/退化合并、wrapSubtitle、清洗、时间码与标签、system 与 queue 归一化、四种导出形状 |
| `compileall`（run-all-tests 第 4 步） | 通过，字节码重定向到 `_scratch/tests/pycache`，不写源码树 |
| `run-all-tests.cmd` | **ALL TESTS PASSED**，五步（引擎 8.6 s / 前端构建 3.1 s / 壳 8.5 s / compileall 0.2 s / 前端单测 0.3 s） |
| 版本一致性 | 5 处均为 1.3.0（`version.txt` / `Cargo.toml` / `tauri.conf.json` / 两个 `package.json`） |
| 便携包构建 | 20,848 个文件 / 10.47 GB；**20/20 项校验通过**（含新加的 `engine\version.txt` 一致性）；`SHA256SUMS.txt` 覆盖 20,847 个文件并校验一致 |
| 便携包启动实测（1.3.1） | 2 s 内出现 `data/port.json`（键恰为 `port,version,pid`，`version=1.3.1`）；`/api/health` 200 且 **`version=1.3.1`**；`/api/system` 无 token → **401**；三份日志中 32 位 hex token 命中 0；`engine\voicetrace_engine` 无 `__pycache__`；强杀后 python 残留 0 |
| 日志卫生 | `engine.log` / `engine.out.log` / `supervisor.log` 三份日志中 32 位 hex token 命中 **0**；无 `panic.log` |
| 进程治理 | 强杀壳后 python 引擎与子孙进程 **0 残留** |
| 引擎冷启动（`run-perf.cmd -EngineOnly`） | `/health` 200 约 1.15 s（不含模型加载） |
| 回归守卫 | `tests/test_regressions_2026_11_17.py`（切句守恒 / cue 钳制 / 编辑检验）+ `tests/test_media_stderr.py`（stderr 死锁 / 卡死看门狗 / 取消） |
| **真实素材转录**（2026-11-17，RTX 4060） | 38.3 分钟 1440p 课程视频（语音覆盖 96%）→ done，822 句 / 13,099 字；阶段 `load 24.2s → extract 3.0s → vad 6.0s → asr 223.9s → finalize`，端到端 **258.6s，RTF 0.113**（纯 ASR 0.097），显存 4.2–5.0GB |
| **真实导出校验** | SRT 820 条：重叠 **0**、零时长 **0**、超 6.5s **0**、行内容 >22 字 **0**、末条 2,296,895ms ≤ 媒体 2,296,945ms、最小条间隔 **正好 40ms**；VTT 头正确；ASS 820 行 Dialogue 与 cue 数一致；TXT 823 行；JSON 1.53MB 可解析 |
| **优雅退出** | 父进程消失后引擎自行退出且 `port.json` 被删除（实测 True/True）；强杀路径下 `port.json` 残留属预期（下次启动按 pid 校验丢弃） |
| **RTF 参数敏感性** | 5 分钟切片、语音 286.8s：`beam=1+batch=12` → RTF 0.100；默认 `beam=3+batch=6` → 0.097。参数不是瓶颈；文档旧值 0.021 不可复现 |
| **长素材转录**（78.4 分钟，同机） | 720p 录播课（语音覆盖 96.8%）→ done，1358 句 / 21,582 字；阶段 `load 14.1s → extract 5.8s → vad 5.8s → asr 324.0s → finalize`，端到端 **360.1s，RTF 0.077**（纯 ASR 0.069）；python 常驻内存稳定 **1.12 GB**；显存 5.7–6.0 GB（4 分钟后进入平台期） |
| **长素材导出校验** | SRT 1355 条：重叠 **0**、零时长 **0**、空文本 **0**、超 6.5s **0**（最长 6,431ms）、单行最宽 **22 字**、末条 4,701,924 ≤ 媒体 4,702,282ms、最小间隔 **40ms**；ASS 1355 行 Dialogue 与 cue 数一致；cue 覆盖媒体 96% |
| **结构化数据健康** | 1358 句全部有 `words`、无时间倒挂、句子起点单调；临时 wav 在 `keep_wav=false` 下被清理（cache 0 文件）；`engine.log` 1.5KB |
| **显存自适应缩批**（1.3.1） | 设 `vram_headroom_mb=4000` 强制缩批：`6 → 3 → 2 → 1`，25 段全部识别完成；RTF 0.217（batch=1 约 2.2× 于 batch=6） |
| **崩溃续跑**（1.3.1） | 78.4 分钟素材在 **35% / 47.7s** 处强杀引擎 → sidecar 保住 **66 段**；重启日志「续跑：已完成 66/239 段」；最终 **1358 句 / 21,568 字**（基线 21,582，差 0.06%）；sidecar 自动清理；无残留进程 |
| **显存争用自愈**（1.3.1） | 续跑期间另一进程占卡、空闲仅 **770 MB** → 自动缩到 batch=1 完成，无 OOM、无数据丢失 |
| **单测规模**（1.3.1） | 引擎 **107**（含 `test_asr_adaptive` 14 + `test_resume` 9）、前端 32、壳 12；`run-all-tests.cmd` 五步全绿 |

## 4. 自动化未覆盖（必须人工验收）

- ~~GPU 真实转录~~ **已实跑**（见 §3，38.3 分钟素材）；仍需人工复核的是字幕**内容**质量（专业术语、数字读法）。
- **>5 小时长视频**回退路径（30 分钟窗口 + 8s 上下文）。
- 拖放导入、双击编辑、↑↓/空格/`Ctrl+Z` 等键位的手感。
- 单实例提示窗口的交互、DPI 125%/150% 与多显示器下的自绘标题栏。
- 模型首次加载 20–90 秒期间的进度显示与取消。

## 5. 已知限制（非缺陷）

- 只在 Windows 11 + RTX 4060 8GB 实测；无 GPU 时回退 CPU，明显变慢。
- 首次加载 4.4 GB AED + 1.2 GB 标点模型需 20–90 秒；同进程内后续任务无需再加载。
- 单个 AED 段 ≤28 秒（模型位置编码 60 秒上限留余量）。
- 未做说话人分离、翻译、字幕烧录。
- 便携包约 10.47 GB（模型 5.19 GB + torch/CUDA 5.09 GB）。
- 数据默认写在 exe 同级 `data/`；不可写时回落 `%LOCALAPPDATA%\VoiceTrace`。
- 无交互桌面（远程服务、CI）无法创建 WebView2，需正常桌面双击。

## 6. 回滚与证据

| 目录 | 用途 |
| --- | --- |
| `_archive/2026-11-19-before-audit-fixes/` | 最近一次审计修复前的改动前快照 |
| `_scratch/` | 一次性测试产物（可删） |

> 更早的快照（`2026-11-03-before-6fix`、`2026-11-17-*`、`2026-11-18-*`）与 `docs/audit-2026-11-17/` 已于 2026-11-19 送回收站；需要时从回收站取回。回滚基线用上面最新快照。

## 7. 出问题怎么报

- 日志：`<便携目录>\data\logs\supervisor.log`、`engine.out.log`、`engine.log`（最后 50 行足够定位）、`panic.log`（壳层异常）。
- 数据目录：`<便携目录>\data`（voicetrace.db、transcripts、thumbs、cache）；不可写时回落 `%LOCALAPPDATA%\VoiceTrace`。
- 便携包内 `使用说明.txt` 有同样的 troubleshooting（含 `0x800700AA`、首次加载慢、杀毒误报）。
- 安全相关问题请按 `SECURITY.md` 的渠道报告。

## 5. 2026-11-18 补充审计修复的自动化验证

> 逐项结论与未实施项见 [`AUDIT-2026-11-19.md`](AUDIT-2026-11-19.md)；早期快照已回收。

| 项 | 结果 |
| --- | --- |
| 引擎 unittest | **135/135 通过**（8.1 s；原 110 + 25 条新回归：迁移/撤销/设置失败、WAV 指纹、单行不丢字、少字长停顿、ffmpeg 时间戳、插字正时长、末条越界、跨窗覆盖、sidecar 边界、首次写库失败、CPU/回滚、同名导出、重复导入、suffix Range、CORS、取消认领、history 深度） |
| 前端单测（`node scripts/test.mjs`） | **45/45 通过**（原 32 + 13：CAS 请求体/undo 参数、409 识别、history 归一、过期回包、cueAt） |
| 前端 `pnpm run typecheck` | 0 错误 |
| 前端 `pnpm run build` | 成功（`dist/` 已更新；构建镜像落在 `_scratch/2026-11-18/` 且已清理） |
| Rust supervisor | **6/6 通过**（`cargo test --release --features custom-protocol --test supervisor`） |
| 构建目录守卫（A7） | `#release\..\#source`、`#release/../#source`、项目根、工作区根 全部 exit 1；默认路径三项 Skip 走 exit 2（不落盘） |
| 未跑 | 真实 GPU 转录、真实壳启动/崩溃、>5h 长视频、DPI/多显示器——仍需真机窗口 |

## 6. 2026-11-18 默认交互与设置页纵向排版

| 项 | 结果 |
| --- | --- |
| 引擎 unittest | **140/140 通过**（8.1 s；新增 5 条：导入默认 `auto_correct=true`、显式 `false` 生效、自动导出落在视频目录、同名不覆盖、关闭时零写入） |
| 前端单测（`node scripts/test.mjs`） | **45/45 通过** |
| 前端 `pnpm typecheck` / `pnpm build` | 0 错误；`dist/` 已更新 |
| 设置页纵向排版（Edge 无头真机预览） | 1600×900 与 1120×900 各截图：单列 4 分区 / 18 项，标签在上、控件在下，无横向溢出、无 console 错误；截图在 `_archive/2026-11-18-settings-shots/` |
| 便携包重打包 | 20,848 文件 / 10.47 GB；**20/20 项校验通过**（exe 版本资源 1.3.2、SHA256 全量一致、engine 无 `__pycache__`）；`data/` 打包前备份、打包后原样还原（设置与库不变） |
| 包内引擎与界面 | `engine\voicetrace_engine` 13 个 `.py` 语法校验通过且含 `_auto_export_srt`；`dist` CSS 已是纵向单列；`使用说明.txt` 已写入新默认说明 |
| 未跑 | 真实 GPU 转录、>5h 长视频、DPI/多显示器——仍需真机窗口 |

## 7. 2026-11-18 后端全量审计与启动回归修复

> 逐项证据、未修项与真机待办见 [`AUDIT-2026-11-19.md`](AUDIT-2026-11-19.md)。

| 项 | 结果 |
| --- | --- |
| 启动回归（用户报告） | 已定位：D4 后 `engine_info.port` 在端口公布前为 0，前端把它当启动失败；壳端口发现 5 s + fallback=0 是放大器。已修：前端 250 ms 轮询 `engine_info`/`engine_status` 最长 180 s，`failed/exited` 立即带根因；壳端口发现 180 s、超时明确失败、重启/失败/退出清 0 |
| 引擎 unittest | **213/213 通过**（14.2 s；新增 73 条：`test_audit_2026_11_18_{api,core,text}.py` 23 + 34 + 16） |
| 前端单测 | **52/52 通过**（新增 7 条：端口 0→真实/failed/exited/超时、contentLen Unicode 标点） |
| 前端 `pnpm typecheck` / `pnpm build` | 0 错误；`dist/` 已重建（主 JS 257.70 kB / gzip 84.67） |
| Rust 壳 | **11 lib + 6 supervisor 通过**（新增 stdout/port.json 优先级、超时 Err、早退秒失败、真实引擎 `bind(0)` 握手 + `/api/health`） |
| 便携包重打包 | 20,848 文件 / 10.47 GB（不含 data）；**20/20 项校验通过**（版本 5 处 1.3.2、exe 版本资源、engine/version、模型、runtime、`SHA256SUMS.txt` 20,847 文件逐文件一致、engine 无 `__pycache__`） |
| 包内修复抽查 | `pipeline.py` 含 `file_missing`；`server.py` 用 `count_chars`；`third_party/.../asr.py` 含 re-raise 注释；exe 5.0 MB（本次重建，嵌入新 dist；与 target/release 及 SHA256SUMS 哈希一致） |
| 便携版引擎冒烟 | 用包内 `runtime\python\python.exe -m voicetrace_engine --port 0`（scratch data）：`port.json={port,version:1.3.2,pid}` 无 token；`/api/health` **200**（0.3 s）；强杀后无残留进程 |
| 运行期 data | 构建前临时备份（818 文件 / 195,494,091 B），构建后原样还原（文件数/字节数一致），备份在验证通过后已清理。**还原后 `-Verify` 的「包内不含 data\」一项会按设计失败**，这是用户库而非打包缺陷 |
| 卫生 | 源码树 `__pycache__` 已清（node `fs.rmSync`）；子代理 `_scratch` 目录已清 |
| 未跑 | 真实 GUI 双击启动（判断依据为 Rust 真引擎握手 + 前端单测 + 构建校验）、真实 GPU 转录、>5h 长视频、DPI/多显示器 |

## 8. 2026-11-19 稳定性全量审计修复

> 逐项结论、复现与未做项见 [`AUDIT-2026-11-19.md`](AUDIT-2026-11-19.md)；改动前快照
> `_archive/2026-11-19-before-audit-fixes/`。

| 项 | 结果 |
| --- | --- |
| 引擎 unittest | **234/234 通过**（14.2s；原 213 + 21 条新回归 `test_audit_2026_11_19.py`：auto_correct 保留合法重叠/超集不变量、edit 英文词边界、retry 保旧稿、fresh 清降级、lowconf、控制字符、零时长回退、karaoke 钳制、保留名、畸形 action） |
| 前端单测 | **52/52 通过**；`pnpm typecheck` 0 错；`node scripts/build.mjs` 重建 dist 成功 |
| Rust 壳 | 本轮未改源码；基线 `cargo test --release --features custom-protocol` **11 lib + 6 supervisor 通过** |
| P0-1 复现守卫 | 理理发/打打电话/用用心/一餐一餐/一时一时 经 cleanup→auto_correct 全链路**不再被误删**；真口吃（老老师/这个这个）仍清；超集差=∅ |
| P1-2 复现守卫 | edit 后 `don't`/`well-known` 保持整词，与 segmentation 分词一致 |
| 脚本静态校验 | build-release/dev/run-perf 内嵌 PS `Parser::ParseFile` 0 错；download_models/pack_smoke/asr.py 及引擎全模块 `py_compile` 通过 |
| 模型白名单干跑 | 源模型全过；空目录负例正确报缺失（预检与 Verify 共用 `Get-ModelProblems`） |
| Python ABI | venv=(3,11)、pyvenv.cfg=3.11、共享 runtime=(3,11) 一致 |
| 交付目录保护 | `#release/data`（69 份转录+DB）**未触碰**；重构建备份逻辑 robocopy 退出码+文件数双校验 |
| 未跑 | 真实 GUI 双击、真实 GPU 转录、>5h 长视频、DPI/多显示器、便携包完整重建（需构建窗口） |

## 9. 2026-11-19 减法施工验收

> 施工依据：「09修复方案（可靠性减法）」；改动前快照 `_archive/2026-11-19-before-subtraction/`。
> 本轮未改 Rust 壳与 `runtime.py`，历史审计文档未改。

| 项 | 结果 |
| --- | --- |
| 引擎 unittest | **233/233 通过**（14.4 s；234 − 2 个 karaoke 测试 + 1 条 <240ms 密集 cue 不重叠断言） |
| 前端单测 | **52/52 通过**；`pnpm typecheck` 0 错；`node scripts/build.mjs` 重建 dist 成功 |
| Rust 壳 | 本轮未改源码（`engine.rs`/`lib.rs`/`win32.rs`/`main.rs` 零改动），**未跑**；沿用 11-19 审计的 11 lib + 6 supervisor 绿 |
| 引擎 compileall | 通过（`PYTHONPYCACHEPREFIX` 重定向到 `_scratch`，源码树 `__pycache__` 已清） |
| B1 WS 静置 | 删空闲判死/heartbeat/online-offline 后，`onclose` 是唯一重连驱动；`tsc` 0 错证明 `"offline"` 无残留引用 |
| B2 karaoke | `ass_karaoke` 请求现在走 `ValueError` → HTTP 400 中文信封；全仓 `#source` 内零命中（dist 已重建） |
| B3 死设置 | 设置项 20→19；全仓 `#source` 内 `vad_max_speech_ms` 零命中；`set(settings) == set(DEFAULT_SETTINGS)` 测试仍绿 |
| B4 auto_retries | 全仓 `auto_retries`/`MAX_AUTO_RETRIES` 零命中；`OomDowngradeTest` 与 fresh 重跑断言改指 `batch_overrides` 后全绿 |
| B5 mock 删除 | `#source/frontend/mock/` 已删（备份在快照里）；`package.json` 解析通过；`src/` 内 `mock` 零命中 |
| C1 cue 钳制 | 新增 `test_gap_wins_even_below_old_hard_floor` 绿；`test_gap_wins_when_cues_are_dense` 改为 `assertEqual(260)`；全仓 `HARD_MIN_CUE_MS` 零命中 |
| C3 量词合表 | `_QUANTIFIER_CLASSIFIERS is MEASURE_CHARS`；重跑 `_scratch/2026-11-19-review/repro_review.py` 的 [D] 段，9 行结果与施工前**完全一致**，超集差=∅ |
| C4 500 信封 | 重跑复现脚本 [A]：`PUT /sentences` lone surrogate → HTTP 500，body 为 `{"error":{"code":"internal","message":"引擎内部错误，详情见 data/logs/engine.log"}}`，**不再回显异常原文**；随后 `GET /transcript` 仍 200（无数据损坏） |
| 复现脚本 [B]/[E] | 导出 srt/ass/json 仍 200；[E] 句窗口未被 `span` 回退撑破（1005 / 句1 1006）。脚本 [C] 因 `to_ass_karaoke` 已删而报 `AttributeError`，**属预期** |
| 净行数 | 26 个源文件 **−150 行**；另删 `frontend/mock/` **1541 行**（server.mjs 1169 + data.mjs 372），合计约 **−1691 行** |
| 便携包重建 | **已完成**：`build-release.ps1` 全量重建（20,848 文件 / 10.47 GB，245.9 s），随后 `-Verify` **22 项全 PASS**，`SHA256SUMS.txt` 20,847 个文件逐文件一致，包内无 `data\` |

### 9.1 真机实跑（2026-11-19 15:20–15:40）

| 项 | 结果 |
| --- | --- |
| 真实 GPU 转录 | 自造 30.4 s 中文语音素材（SAPI Huihui + ffmpeg），引擎日志 `模型设备：cuda`、`use_half=True`；端到端 done，6 句 / `stats` 正常；`auto_export` 把 `speech.srt` 写到视频同目录 |
| 导出契约（真实转录） | srt/vtt/ass/txt/json 全部 200；SRT 6 条 cue：**重叠 0、零时长 0、越媒体时长 0、最小间隔正好 40 ms**；`ass_karaoke` → **400 + 中文信封** |
| 设置面 | `GET /settings` **19 键**、无 `vad_max_speech_ms`；`PUT` 带未知键被忽略；设置页实际渲染无「最长语音段」控件（`.settings-screen` 内文本断言） |
| B1 静置（浏览器 + 真实引擎 + 构建后 dist） | 打开已完成任务后静置 **85 s**：WebSocket **只建立 1 条、0 次关闭**；连接徽标始终 `conn-dot online`，无「重连中/正在连接/连接中断」；编辑器与字幕保持挂载 |
| 草稿与撤销 | 静置后双击句子编辑保存成功，`Ctrl+Z` 撤销回原文；期间无 `transcriptError`、无 `JobErrorView` 顶掉编辑器 |
| 未保存草稿守卫 | 编辑一条句子留草稿后点返回：原生确认框文案「当前句子有未保存的修改，切换将丢弃。继续？」；取消后仍留在编辑器（P1-6 未被破坏） |
| 离线模拟 | `context.setOffline(true)` 4 s 后徽标仍 `online`、无「连接中断」文案；恢复联网后正常 |
| 密集 cue（引擎 + 前端） | 句 0 `1000–1005` / 句 1 `1006–2000`：引擎 `build_cues` 产出 `[(1000,1001),(1006,2000)]`，**不重叠、不越界**；前端 `buildSubtitleCues` 按 `MERGE_CLOSER_THAN_MS` 合并为 1 条，同样不重叠 |
| 便携版双击启动 | 新包 `声迹.exe` 双击启动成功，首页底部显示「**引擎在线 · CUDA**」；关闭后无残留 `声迹.exe`/python（数据写到隔离目录，包内仍无 `data\`） |
| 证据 | `_scratch/2026-11-19-gui/`（脚本、导出、转录 JSON、4 张截图、`check_exports.py`、`ui_check.mjs`） |
| 未跑 | 物理拔网线/飞行模式（以浏览器离线模拟替代）、>5 小时长视频回退、DPI 125%/150% 与多显示器、真实 GPU 长素材（本轮 30 s 是真实 GPU，但未重跑长素材）、便携包内真实转录（引擎与 dist 与开发环境同源，抽验以启动+在线为准） |
