# 声迹 VoiceTrace

Windows 本地视频 / 音频转字幕工作台：词级时间轴、逐句校对，导出 SRT / VTT / ASS / TXT / JSON。
全部推理在本机完成，不上传任何媒体或文本。

形态：Tauri 2 壳（Rust）+ React 18 前端 + 本地 Python 引擎（FastAPI，FireRedASR2-AED + FireRedVAD + FireRedPunc）。

## 功能

- 视频 / 音频导入，本机 GPU（CUDA）优先，纯 CPU 也可运行
- 词级时间轴，逐句编辑、撤销、批量替换
- 口吃清理、语气词清理、同音异写建议
- 转录后自动导出同名字幕；手动导出 SRT / VTT / ASS / TXT / JSON
- 批量队列、任务级断点续跑、显存压力自动降级
- 离线运行：模型可在应用内一键下载，数据只保存在本机

## 系统要求

- Windows 10/11 x64（仅在 Windows 11 + RTX 4060 上实测）
- 可选 NVIDIA GPU；CPU 推理可用但明显更慢
- 首次使用需在应用内下载两块内容：
  - 推理组件（CUDA 版 PyTorch，约 2.6 GB，设置 → 推理组件）
  - 离线模型（约 5.2 GB，设置 → 离线模型）

从源码构建还需要：Node.js 24 + pnpm 11、Python 3.11、Rust stable、Microsoft Edge WebView2 Runtime、
`ffmpeg.exe` / `ffprobe.exe`（或设置环境变量 `VT_FFMPEG` 指向其目录）。

## 模型与推理组件

安装包只带应用壳、引擎和基础 Python 运行时，**不含 torch 与模型**（两者合计约 7.7 GB，超出安装器上限）。
首次启动后到「设置」完成两步：

1. **推理组件**：下载 CUDA 版 PyTorch（torch 2.1.0+cu118 + torchaudio，约 2.6 GB），支持官方源 / 阿里云镜像与本地代理。
2. **离线模型**：下载三个 FireRedASR2S 权重（约 5.2 GB），支持 `hf-mirror.com` 镜像与本地代理。

两步都支持取消与续传。完成后重启引擎即可离线转录。

三个模型均来自 [FireRedASR2S](https://github.com/FireRedTeam/FireRedASR2S) 的开源权重：

| 目录 | Hugging Face 仓库 | 用途 |
| --- | --- | --- |
| `models/aed` | `FireRedTeam/FireRedASR2-AED` | 语音识别 |
| `models/vad` | `FireRedTeam/FireRedVAD` | 语音活动检测 |
| `models/punc` | `FireRedTeam/FireRedPunc` | 标点恢复 |

命令行方式（开发环境）：

```powershell
cd engine
.\.venv\Scripts\python.exe -m voicetrace_engine.model_download --models-dir models
```

## 开发

```powershell
# 1) 引擎
cd engine
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m voicetrace_engine.model_download --models-dir models

# 2) 前端
cd ..\frontend
pnpm install
pnpm dev

# 3) 带壳联调（另开终端，项目根执行）
pwsh -NoProfile -File scripts\dev.ps1
```

引擎单独启动：

```powershell
cd engine
.\.venv\Scripts\python.exe -m voicetrace_engine --port 8765 --token dev `
  --data-dir ..\_scratch\data --models-dir .\models --ffmpeg <ffmpeg-bin> --engine-root .
```

## 构建

```powershell
# 前端产物
cd frontend
pnpm install
pnpm build

# Tauri 壳（GNU 工具链；PATH 需含 mingw64\bin，windres 编译清单用）
cd ..\app\src-tauri
cargo build --release --features custom-protocol
```

壳的产物在 `app/src-tauri/target/release/`（`voicetrace.exe` + `WebView2Loader.dll`）。
注意：`pnpm run build:installer`（tauri build）只产**不含引擎/运行时**的 NSIS 壳，不是交付物。

**完整安装包**（含引擎与基础 Python 运行时，不含 torch 与模型）由 `scripts/build-installer.ps1`
组装并调 NSIS 构建，脚本为 `scripts/installer.nsi`：

- 需要本机有一份现成便携布局可复用运行时（`-RuntimeSource`，默认 `D:\VoiceTrace`，
  即已安装的本应用；全新机器先用 `scripts/build-release.ps1` 生成）。
- 需要 NSIS 3.x（`makensis.exe`，Tauri 缓存自带：`%LOCALAPPDATA%\tauri\NSIS`）。
- 产物 `VoiceTrace_<版本>_x64-setup.exe`：per-user 安装，支持**覆盖升级**——
  从注册表定位既有安装目录，只覆盖程序本体，已下载的模型、用户数据与 torch 原地保留。

免安装的完整便携目录（含 Python 引擎、模型与 ffmpeg 运行时）由 `scripts/build-release.ps1` 组装，
需要本机具备 Python 3.11 运行时与 ffmpeg，产物体积约 10 GB。

## 测试

```powershell
# 引擎
cd engine
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v

# 前端
cd ..\frontend
pnpm typecheck
pnpm test

# 壳
cd ..\app\src-tauri
cargo test
```

## 目录结构

| 路径 | 说明 |
| --- | --- |
| `version.txt` | 版本唯一真源（单行 `x.y.z`） |
| `app/` | Tauri 2 壳：拉起引擎、随机端口、一次性 token、单实例、Job Object |
| `frontend/` | React 18 + Vite + TS 前端 |
| `engine/` | Python 引擎（FastAPI）；`models/` 与 `.venv/` 不入库 |
| `engine/third_party/FireRedASR2S/` | 上游推理代码（Apache-2.0，含本地补丁） |
| `scripts/` | `build-release.ps1`（便携包）、`installer.nsi` + `build-installer.ps1`（安装包）、`dev.ps1`、模型下载等 |
| `docs/` | `SPEC.md` 协议契约、`VERIFY.md` 验收清单、审计报告 |
| `assets/` | 应用图标 |

## 第三方组件

- [FireRedASR2S](https://github.com/FireRedTeam/FireRedASR2S) — Apache-2.0，许可证见 `engine/third_party/FireRedASR2S/LICENSE`
- FireRedASR2-AED / FireRedVAD / FireRedPunc 模型权重 — 版权归上游，使用时请遵守各自仓库的许可

## 许可证

[MIT](LICENSE)
