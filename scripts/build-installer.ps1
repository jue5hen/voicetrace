# ============================================================================
# 声迹 VoiceTrace — 安装包组装与构建脚本
#
# 产物：VoiceTrace-<版本>-setup.exe（NSIS，per-user，可覆盖升级）
#
# 前提：
#   1. Rust 壳已编译：app\src-tauri\target\release\voicetrace.exe
#      （cargo build --release --features custom-protocol，PATH 需含 mingw64\bin）
#   2. 前端已构建：frontend\dist（壳编译时已内嵌，此处仅断言存在）
#   3. 有一份现成的便携布局可复用运行时（默认 D:\VoiceTrace，即已安装的本应用）：
#      runtime\python、runtime\site-packages、runtime\ffmpeg.exe / ffprobe.exe。
#      全新机器请先用 scripts\build-release.ps1 生成便携布局。
#   4. NSIS 3.x（makensis.exe，Tauri 缓存自带：%LOCALAPPDATA%\tauri\NSIS）。
#
# 覆盖升级不重新下载模型的关键：payload 只含程序本体与基础运行时，
# 不含 models\、data\、runtime\.downloads\，也不含 torch / torchaudio /
# functorch / torchgen 及其 dist-info —— 安装时 File /r 只覆盖 payload 内文件，
# 既有模型、用户数据与已安装的 torch 原地保留。
#
# 用法（pwsh 7）：
#   .\scripts\build-installer.ps1                       # 默认参数
#   .\scripts\build-installer.ps1 -RuntimeSource 'D:\VoiceTrace' -OutFile 'D:\out\vt-setup.exe'
# ============================================================================

[CmdletBinding()]
param(
    [string]$SourceRoot    = (Split-Path -Parent $PSScriptRoot),
    [string]$RuntimeSource = 'D:\VoiceTrace',
    [string]$StageDir      = 'D:\Download\_vt-build',
    [string]$OutFile       = '',
    [string]$MakeNsis      = "$env:LOCALAPPDATA\tauri\NSIS\makensis.exe"
)

$ErrorActionPreference = 'Stop'

function Assert-Path([string]$p, [string]$what) {
    if (-not (Test-Path -LiteralPath $p)) { throw "缺少${what}：$p" }
}

function Invoke-Robocopy([string[]]$args_) {
    & robocopy.exe @args_ | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败（退出码 $LASTEXITCODE）：$($args_ -join ' ')" }
    $global:LASTEXITCODE = 0
}

# ---------------------------------------------------------------- 版本与前提
$version = (Get-Content -LiteralPath (Join-Path $SourceRoot 'version.txt') -Raw).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw "version.txt 内容异常：$version" }
if (-not $OutFile) { $OutFile = Join-Path $StageDir "VoiceTrace_${version}_x64-setup.exe" }

$exeSrc = Join-Path $SourceRoot 'app\src-tauri\target\release\voicetrace.exe'
Assert-Path $exeSrc 'Rust 壳 release 产物（先 cargo build --release --features custom-protocol）'
Assert-Path (Join-Path $SourceRoot 'frontend\dist\index.html') '前端构建产物（先 pnpm build）'
Assert-Path (Join-Path $SourceRoot 'engine\voicetrace_engine\server.py') '引擎源码'
Assert-Path (Join-Path $SourceRoot 'engine\version.txt') 'engine\version.txt'
Assert-Path (Join-Path $RuntimeSource 'runtime\python\python.exe') '可复用运行时（-RuntimeSource 指向便携布局根目录）'
Assert-Path (Join-Path $RuntimeSource 'runtime\site-packages\numpy') '可复用 site-packages'
Assert-Path (Join-Path $RuntimeSource 'runtime\ffmpeg.exe') '可复用 ffmpeg'
Assert-Path $MakeNsis 'makensis.exe（NSIS 3.x）'

$engineVer = (Get-Content -LiteralPath (Join-Path $SourceRoot 'engine\version.txt') -Raw).Trim()
if ($engineVer -ne $version) { throw "engine\version.txt（$engineVer）与根 version.txt（$version）不一致" }

$payload = Join-Path $StageDir 'payload'
if (Test-Path -LiteralPath $payload) { Remove-Item -LiteralPath $payload -Recurse -Force }
New-Item -ItemType Directory -Path $payload -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $OutFile) -Force | Out-Null

Write-Host "==> 组装 payload（版本 $version）"

# ---------------------------------------------------------------- 壳与加载器
Copy-Item -LiteralPath $exeSrc -Destination (Join-Path $payload '声迹.exe')
$loader = Join-Path $SourceRoot 'app\src-tauri\target\release\WebView2Loader.dll'
if (-not (Test-Path -LiteralPath $loader)) {
    $loader = Join-Path $RuntimeSource 'WebView2Loader.dll'   # 同版本构建产物，可复用
}
Assert-Path $loader 'WebView2Loader.dll'
Copy-Item -LiteralPath $loader -Destination (Join-Path $payload 'WebView2Loader.dll')

# ---------------------------------------------------------------- 引擎源码
# 与已安装布局一致：voicetrace_engine / third_party / scripts / version.txt，
# 不带 tests 与缓存。
Invoke-Robocopy @(
    (Join-Path $SourceRoot 'engine'), (Join-Path $payload 'engine'), '/E', '/NFL', '/NDL', '/NJH', '/NJS',
    '/XD', 'tests', '__pycache__', '.venv', '.pytest_cache', '.cache', 'models',
    '/XF', '*.pyc', '*.pyo', 'requirements.txt'
)

# ---------------------------------------------------------------- 运行时
Invoke-Robocopy @(
    (Join-Path $RuntimeSource 'runtime\python'), (Join-Path $payload 'runtime\python'), '/E', '/NFL', '/NDL', '/NJH', '/NJS',
    '/XD', '__pycache__', '/XF', '*.pyc'
)
# 基础依赖全量保留（torch 的 sympy/networkx/jinja2/filelock 等依赖在内），
# 仅排除 torch 本体与其 dist-info —— 覆盖安装时不触碰用户已下载的 torch。
Invoke-Robocopy @(
    (Join-Path $RuntimeSource 'runtime\site-packages'), (Join-Path $payload 'runtime\site-packages'), '/E', '/NFL', '/NDL', '/NJH', '/NJS',
    '/XD', 'torch', 'torchaudio', 'functorch', 'torchgen', 'torch-*.dist-info', 'torchaudio-*.dist-info', '__pycache__',
    '/XF', '*.pyc'
)
Copy-Item -LiteralPath (Join-Path $RuntimeSource 'runtime\ffmpeg.exe')  -Destination (Join-Path $payload 'runtime\ffmpeg.exe')
Copy-Item -LiteralPath (Join-Path $RuntimeSource 'runtime\ffprobe.exe') -Destination (Join-Path $payload 'runtime\ffprobe.exe')

# ---------------------------------------------------------------- 文档与版本
Copy-Item -LiteralPath (Join-Path $SourceRoot 'LICENSE') -Destination (Join-Path $payload 'LICENSE.txt')
Set-Content -LiteralPath (Join-Path $payload 'version.txt') -Value $version -Encoding utf8NoBOM -NoNewline:$false

$readme = @"
声迹 VoiceTrace — 安装版 $version
==================================

一、启动
  从开始菜单「声迹 VoiceTrace」或安装目录下的「声迹.exe」启动。
  首次启动需要加载本地模型，通常 20–90 秒（显卡较弱或杀毒在扫描时更久），
  启动页会显示进度；失败时会显示引擎日志的最后 50 行。
  若 models 目录为空，启动后到「设置 → 离线模型」点「下载缺失模型」（约 5.2 GB，
  支持 hf-mirror.com 镜像与本地代理，可随时取消并续传）。
  覆盖升级：直接运行新版安装包并选择既有安装目录即可；已下载的模型、
  用户数据与推理组件（torch）全部保留，无需重新下载。

二、使用
  1. 把视频文件拖进窗口，或按 Ctrl+O 选择文件。
  2. 等待自动转录（VAD → ASR → 标点）；一次只跑一条，其余排队。
     新任务默认开启「全部自动校正」：完成后自动清理重复口吃与高置信同音异写（可逐条关闭）。
  3. 左侧媒体预览占满工作区；右侧「字幕」标签逐句校对（单击定位、双击编辑、↑↓ 切换）。
  4. 顶部标签切到「校对建议」，处理疑惑词、重复词、低置信句。
  5. 转录完成会自动把同名 SRT 保存到视频所在目录（同名文件已存在时追加序号，不覆盖）；
     也可手动导出 SRT / VTT / ASS / TXT / JSON。
  6. 设置是独立页面（Ctrl+,，纵向单列）；保存后重启仍保留。

三、数据目录与回落规则
  - 默认数据目录：安装目录下的 data（数据库、字幕、设置、日志、WebView 缓存）。
  - 回落规则：若 exe 同级 data 不可写（放在只读盘、程序目录被策略限制等），
    自动回落到 %LOCALAPPDATA%\VoiceTrace；也可用环境变量 VOICETRACE_DATA_DIR 显式指定。
  - 日志全部在「数据目录\logs」下，共四份：
      supervisor.log    壳的监督日志（引擎启动/退出/重启、退出码）
      engine.log        引擎当前日志（含引擎标准输出与标准错误）
      engine.prev.log   上一次运行的引擎日志（重启时轮转，便于对照崩溃前状态）
      panic.log         壳崩溃时的 panic 记录（正常运行为空或不存在）
  - 全部推理在本机完成，不上传任何视频或文本。

四、目录说明
  声迹.exe                        主程序
  WebView2Loader.dll              WebView2 加载器（GNU 构建产物，必须与 exe 同级）
  engine\voicetrace_engine\       Python 引擎源码
  engine\third_party\FireRedASR2S\ FireRedASR2S 源码（转录必需）
  models\{aed,vad,punc}\          本地模型（可在应用内下载）
  runtime\python\                 独立 CPython 3.11
  runtime\site-packages\          Python 依赖（torch 在应用内另行下载安装）
  runtime\ffmpeg.exe              音视频处理
  uninstall.exe                   卸载程序
  使用说明.txt                    本文件

五、注意
  - 不要单独移动 exe；engine、models、runtime 必须与它同级，并保持内部目录结构不变。
  - 目录可以放在含中文或空格的路径下；不建议放进会被同步/虚拟化的目录（如 OneDrive）。
  - 首次运行若被 SmartScreen 拦截，选择「更多信息 → 仍要运行」。
  - 若杀毒软件误报，请把整个目录加入信任（便携 Python 与 torch 的动态库常被误判）。
  - 同一时间只运行一个实例（见六）。
  - 卸载：Windows「设置 → 应用 → 安装的应用」找到「声迹 VoiceTrace」，
    或运行安装目录下的 uninstall.exe。默认保留 models / data / runtime
    （含已下载的 torch），重装后可直接使用；也可在卸载时选择全部删除。

六、常见故障
  1) 启动即报错，错误码 0x800700AA（请求的资源在使用中）：
     多为同时开了两个实例，或上一实例尚未完全退出，WebView2 用户数据目录被占用。
     处理：在任务管理器结束所有「声迹.exe」与本目录下的 python.exe，再重新启动。
  2) 一直停在启动页、超过 90 秒：
     首次加载模型本身就慢（模型约 5.2 GB，torch 首次初始化要建 CUDA 上下文），先等满 2 分钟；
     仍不动就看 supervisor.log 与 engine.log 末尾，确认 runtime\python\python.exe 存在。
     提示缺少 DLL 时，安装 Microsoft Edge WebView2 Runtime（Win11 通常已带）。
  3) 杀毒软件报警或 exe 被删/被隔离：
     本程序未做代码签名，便携 Python 与 torch 的动态库容易被启发式误判。
     处理：还原文件并把整个目录加入信任；企业环境请让 IT 放行。
  4) 转录没有声音：
     确认视频本身有音轨、runtime\ffmpeg.exe 未被删除或改名。
  5) 需要反馈问题：
     附上本说明第一行的版本号，加 supervisor.log 与 engine.log 的最后 50 行。
     日志里不应包含 token；如发现异常字符串，请先脱敏再外发。
"@
Set-Content -LiteralPath (Join-Path $payload '使用说明.txt') -Value $readme -Encoding utf8NoBOM

# ---------------------------------------------------------------- 组装断言
Assert-Path (Join-Path $payload '声迹.exe') 'payload 主程序'
Assert-Path (Join-Path $payload 'engine\voicetrace_engine\server.py') 'payload 引擎'
Assert-Path (Join-Path $payload 'engine\version.txt') 'payload 引擎版本文件'
Assert-Path (Join-Path $payload 'runtime\python\python.exe') 'payload Python'
Assert-Path (Join-Path $payload 'runtime\site-packages\numpy') 'payload 基础依赖'
if (Test-Path -LiteralPath (Join-Path $payload 'runtime\site-packages\torch')) {
    throw 'payload 误含 torch —— 覆盖安装会破坏用户已安装的推理组件'
}
$payloadSize = (Get-ChildItem -LiteralPath $payload -Recurse -File -Force | Measure-Object Length -Sum).Sum
Write-Host ('==> payload 就绪：{0:N0} MB' -f ($payloadSize / 1MB))

# ---------------------------------------------------------------- NSIS
Write-Host '==> makensis 压缩构建（LZMA solid，约需几分钟）'
$nsi = Join-Path $SourceRoot 'scripts\installer.nsi'
& $MakeNsis /V2 "/DVERSION=$version" "/DPAYLOAD=$payload" "/DOUTFILE=$OutFile" $nsi
if ($LASTEXITCODE -ne 0) { throw "makensis 失败（退出码 $LASTEXITCODE）" }
Assert-Path $OutFile '安装包产物'

$size = (Get-Item -LiteralPath $OutFile).Length
$hash = (Get-FileHash -LiteralPath $OutFile -Algorithm SHA256).Hash
Write-Host ('==> 完成：{0}（{1:N1} MB）' -f $OutFile, ($size / 1MB))
Write-Host "    SHA256: $hash"