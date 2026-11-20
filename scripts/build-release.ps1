#requires -Version 5.1
<#
  声迹 VoiceTrace - 便携版打包脚本（幂等、可重跑、可校验）

  常用：
    pwsh -NoProfile -File #source\scripts\build-release.ps1                        全量构建 + 收尾自动校验
    pwsh -NoProfile -File #source\scripts\build-release.ps1 -Verify                只校验现有便携包（不构建、不拷贝）
    pwsh -NoProfile -File #source\scripts\build-release.ps1 -BumpVersion 1.3.0     一次改写 5 处版本号后退出
    pwsh -NoProfile -File #source\scripts\build-release.ps1 -SkipFrontend          复用已有 dist
    pwsh -NoProfile -File #source\scripts\build-release.ps1 -SkipCargo             复用已有 release exe
    pwsh -NoProfile -File #source\scripts\build-release.ps1 -ReleaseDir <目录>     输出到别处（默认 #release\声迹_portable）

  退出码：
    0  成功（构建并校验通过 / -Verify 通过 / -BumpVersion 完成）
    1  失败（预检、构建、robocopy、校验任一环节失败）
    2  明确未产出可交付包（-SkipFrontend -SkipCargo -SkipPayload 三项全给；脚本不做任何构建/拷贝）

  版本单一真源： #source\version.txt（单行 x.y.z）
    每次构建开始断言 5 处一致，任一不符立即 throw 并指出是哪一处：
      #source\version.txt
      #source\app\src-tauri\Cargo.toml（只认 [package] 段的 version，不碰依赖版本）
      #source\app\src-tauri\tauri.conf.json
      #source\frontend\package.json
      #source\app\package.json

  产物：<repo>/#release/声迹_portable/
    声迹.exe  WebView2Loader.dll  使用说明.txt  SHA256SUMS.txt
    engine\voicetrace_engine\  engine\third_party\FireRedASR2S\
    models\{aed,vad,punc}\
    runtime\python\  runtime\site-packages\  runtime\ffmpeg.exe  runtime\ffprobe.exe

  硬约束：
    - data\ 是运行期产物（数据库/字幕/日志/WebView 缓存），每次构建先整目录备份到
      _archive\<日期>-release-data\ 再删除（备份失败即中止），绝不进包。
    - 拷 payload 前先清目标端 engine\ models\ runtime\，再 /MIR 复制，保证幂等（不残留旧文件）。
    - robocopy 退出码 >= 4 一律 throw（4–7 = 有文件未同步，8+ = 失败），每次打印可读摘要。
    - 递归删除统一走 node 的 fs.rmSync（工作区约定，不用 rm -rf / Remove-Item -Recurse）。
#>
[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$SkipCargo,
    [switch]$SkipPayload,
    [switch]$Verify,
    [string]$BumpVersion,
    [string]$ReleaseDir,
    [string]$PythonRuntime,
    [string]$FfmpegDir
)

$ErrorActionPreference = 'Stop'
$NL  = [string][char]10
$CRLF = [string][char]13 + [string][char]10

$root           = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$sourceDir      = Join-Path $root '#source'
$versionFile    = Join-Path $sourceDir 'version.txt'
$frontend       = Join-Path $sourceDir 'frontend'
$app            = Join-Path $sourceDir 'app'
$srcTauri       = Join-Path $app 'src-tauri'
$cargoToml      = Join-Path $srcTauri 'Cargo.toml'
$tauriConf      = Join-Path $srcTauri 'tauri.conf.json'
$frontendPkg    = Join-Path $frontend 'package.json'
$appPkg         = Join-Path $app 'package.json'
$engineDir      = Join-Path $sourceDir 'engine'
$venvSitePackages = Join-Path $engineDir '.venv\Lib\site-packages'
$exeSource      = Join-Path $srcTauri 'target\release\voicetrace.exe'
$webviewDllSource = Join-Path $srcTauri 'target\release\WebView2Loader.dll'
$exeName        = '声迹.exe'
$releaseDefault = Join-Path $root '#release\声迹_portable'

# ---- 输出目录 ----------------------------------------------------------------
if ($ReleaseDir) {
    # 8.3 短名（如 #SOURCE~1）能绕过字符串级路径守卫，含 '~' 的路径段一律拒绝。
    if (@($ReleaseDir -split '[\\/]' | Where-Object { $_ -like '*~*' }).Count -gt 0) {
        throw "-ReleaseDir 路径段不能含 '~'（8.3 短名可能绕过路径守卫）：$ReleaseDir"
    }
    if ([System.IO.Path]::IsPathRooted($ReleaseDir)) { $release = $ReleaseDir } else { $release = Join-Path $root $ReleaseDir }
} else {
    $release = $releaseDefault
}
$release = $release.TrimEnd('\')

# 先规范化再比较：正斜线、..、大小写变体都不能绕过。旧实现只做小写字符串
# 比较，"#release\..\#source" 会被判成合法，全量构建随后删掉真实源码。
function Resolve-FullPath([string]$path) {
    return [System.IO.Path]::GetFullPath($path).TrimEnd('\', '/')
}
function Test-PathInside([string]$child, [string]$parent) {
    $c = $child.TrimEnd('\', '/')
    $p = $parent.TrimEnd('\', '/')
    if ($c.Equals($p, [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
    $sep = [System.IO.Path]::DirectorySeparatorChar
    return ($c.StartsWith($p + $sep, [System.StringComparison]::OrdinalIgnoreCase) -or
            $c.StartsWith($p + '/', [System.StringComparison]::OrdinalIgnoreCase))
}
$rootFull    = Resolve-FullPath $root
$sourceFull  = Resolve-FullPath $sourceDir
$releaseFull = Resolve-FullPath $release
# 已存在的目录再用 FullName 规范化一次（消掉大小写/尾分隔符变体），然后才做守卫比较。
if (Test-Path -LiteralPath $releaseFull) {
    $releaseFull = Resolve-FullPath (Get-Item -LiteralPath $releaseFull -Force).FullName
}
if ($releaseFull.Equals($rootFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "-ReleaseDir 不能是项目根：$releaseFull"
}
if (Test-PathInside $releaseFull $sourceFull) {
    throw "-ReleaseDir 不能位于 #source 内（会污染源码）：$releaseFull"
}
if (Test-PathInside $sourceFull $releaseFull) {
    throw "-ReleaseDir 不能是 #source 的上级（删除目标会覆盖源码）：$releaseFull"
}
$release = $releaseFull

# ---- 工具链 ------------------------------------------------------------------
$cargoBin = Join-Path $root '..\_tools\cargo\bin'
if ((Test-Path (Join-Path $cargoBin 'cargo.exe')) -and ($env:PATH -notlike "*$cargoBin*")) {
    $env:PATH = "$cargoBin;$env:PATH"
}

function Resolve-FfmpegDir {
    if ($FfmpegDir) { return $FfmpegDir }
    $candidates = New-Object System.Collections.ArrayList
    if ($env:VT_FFMPEG) { [void]$candidates.Add($env:VT_FFMPEG) }
    $cmd = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
    if ($cmd) { [void]$candidates.Add((Split-Path -Parent $cmd.Source)) }
    [void]$candidates.Add((Join-Path $root '..\_tools\ffmpeg\bin'))
    [void]$candidates.Add('D:\ffmpeg\bin')
    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c 'ffmpeg.exe'))) { return $c }
    }
    return 'D:\ffmpeg\bin'
}

function Resolve-PythonRuntime {
    if ($PythonRuntime) { return $PythonRuntime }
    $shared = Join-Path $root '..\_tools\python-3.11.16'
    if (Test-Path (Join-Path $shared 'python.exe')) { return $shared }
    $uv = Join-Path $env:APPDATA 'uv\python\cpython-3.11-windows-x86_64-none'
    if (Test-Path (Join-Path $uv 'python.exe')) { return $uv }
    return $uv
}

$ffmpegDirResolved    = Resolve-FfmpegDir
$pythonRuntimeResolved = Resolve-PythonRuntime

# ---- 计时与步骤 --------------------------------------------------------------
$script:stepNo = 0
$script:dataBackupDir = $null
$script:total = [System.Diagnostics.Stopwatch]::StartNew()

function Step([string]$name, [scriptblock]$body) {
    $script:stepNo++
    Write-Host ""
    Write-Host ("[{0}] {1}" -f $script:stepNo, $name) -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    & $body
    $sw.Stop()
    Write-Host ("    完成，用时 {0:n1}s" -f $sw.Elapsed.TotalSeconds) -ForegroundColor DarkGray
}

function Assert-File([string]$path, [string]$what) {
    if (-not (Test-Path -LiteralPath $path)) { throw "缺少$what：$path" }
}

# ---- 递归删除（工作区约定：只能用 node fs.rmSync） ----------------------------
function Remove-Tree([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
        throw "找不到 node.exe，无法递归删除 $path（工作区约定：递归删除只能走 node fs.rmSync）"
    }
    $env:VT_RM_PATH = $path
    try {
        & node.exe -e "const fs=require('fs');fs.rmSync(process.env.VT_RM_PATH,{recursive:true,force:true,maxRetries:3,retryDelay:300});"
        if ($LASTEXITCODE -ne 0) { throw "递归删除失败（node exit $LASTEXITCODE）：$path" }
    } finally {
        Remove-Item Env:\VT_RM_PATH -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $path) { throw "递归删除后目录仍存在：$path" }
    Write-Host ("    已删除 {0}" -f $path) -ForegroundColor DarkGray
}

# ---- robocopy（严格退出码 + 可读摘要） ---------------------------------------
function Invoke-Robocopy([string]$source, [string]$dest, [string]$label) {
    if (-not (Test-Path -LiteralPath $source)) { throw "缺少目录：$source" }
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    $rcArgs = @($source, $dest, '/MIR', '/MT:16', '/R:2', '/W:2', '/NP', '/NFL', '/NDL', '/NJH')
    if ($label -eq 'models/punc') {
        # punc 的 TensorFlow 权重与 PyTorch 推理无关，排除可省约 454 MB。
        $rcArgs += @('/XF', 'tf_model.h5')
    }
    # __pycache__ 等垃圾目录对所有 label 生效（runtime/site-packages 里也有 __pycache__）。
    $rcArgs += @('/XD', '__pycache__', '.pytest_cache', '.git', '.mypy_cache')
    Write-Host ("    robocopy {0}" -f $label) -ForegroundColor DarkGray
    $out = & robocopy.exe @rcArgs 2>&1
    $code = $LASTEXITCODE
    $lines = @($out | ForEach-Object { [string]$_ })

    $summary = @()
    foreach ($line in $lines) {
        if ($line -match '^\s*(Dirs|Files|Bytes)\s*:\s+(.+)$') {
            $summary += ($line.Trim() -replace '\s+', ' ')
        }
    }
    if ($summary.Count -gt 0) {
        Write-Host ("      " + ($summary -join ' | ')) -ForegroundColor DarkGray
    } else {
        foreach ($l in @($lines | Select-Object -Last 6)) { Write-Host ("      " + $l) -ForegroundColor DarkGray }
    }

    if ($code -ge 8) {
        $detail = ($lines | Select-Object -Last 20 | Out-String)
        throw ("robocopy 失败（exit {0}）：{1} -> {2}" -f $code, $source, $dest) + ($NL + $NL) + $detail
    }
    if ($code -ge 4) {
        $detail = ($lines | Select-Object -Last 20 | Out-String)
        throw ("robocopy 有文件未同步（exit {0}，4–7 = 部分不匹配/不可访问，不再静默放过）：{1} -> {2}" -f $code, $source, $dest) + ($NL + $NL) + $detail + '请检查目标端文件占用（关掉正在运行的程序）与权限后重跑。'
    }
}

# ---- 版本号：真源 + 5 处一致性 + 改写 ----------------------------------------
function Get-SourceVersion {
    if (-not (Test-Path -LiteralPath $versionFile)) { throw "缺少版本真源：$versionFile" }
    $v = ([System.IO.File]::ReadAllText($versionFile)).Trim()
    if ($v -notmatch '^\d+\.\d+\.\d+$') {
        throw ("版本真源格式非法（应为 x.y.z 单行）：{0} -> '{1}'" -f $versionFile, $v)
    }
    return $v
}

function Get-FileText([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { throw "缺少文件：$path" }
    $bytes = [System.IO.File]::ReadAllBytes($path)
    $bom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    return @{ Text = [System.IO.File]::ReadAllText($path); Bom = $bom }
}

function Write-FileText([string]$path, [string]$text, [bool]$bom) {
    [System.IO.File]::WriteAllText($path, $text, (New-Object System.Text.UTF8Encoding($bom)))
}

# 返回 @{ Text; Offset; Length; Prefix; Suffix; Value; Bom }
function Find-VersionField([string]$path, [string]$kind) {
    $file = Get-FileText $path
    $text = $file.Text
    if ($kind -eq 'cargo') {
        $pkg = [regex]::Match($text, '(?m)^[ \t]*\[package\][ \t]*\r?$')
        if (-not $pkg.Success) { throw "Cargo.toml 缺少 [package] 段：$path" }
        $base = $pkg.Index + $pkg.Length
        $m = [regex]::Match($text.Substring($base), '(?m)^([ \t]*)version[ \t]*=[ \t]*"([^"]*)"')
        if (-not $m.Success) { throw "Cargo.toml 的 [package] 段缺少 version：$path" }
        return @{
            Text   = $text
            Offset = $base + $m.Index
            Length = $m.Length
            Prefix = $m.Groups[1].Value + 'version = "'
            Suffix = '"'
            Value  = $m.Groups[2].Value
            Bom    = $file.Bom
        }
    }
    $m = [regex]::Match($text, '"version"\s*:\s*"([^"]*)"')
    if (-not $m.Success) { throw "找不到 version 字段：$path" }
    $groupStart = $m.Groups[1].Index - $m.Index
    return @{
        Text   = $text
        Offset = $m.Index
        Length = $m.Length
        Prefix = $m.Value.Substring(0, $groupStart)
        Suffix = $m.Value.Substring($groupStart + $m.Groups[1].Length)
        Value  = $m.Groups[1].Value
        Bom    = $file.Bom
    }
}

$versionTargets = @(
    @{ Label = '#source\app\src-tauri\Cargo.toml';      Path = $cargoToml;   Kind = 'cargo' },
    @{ Label = '#source\app\src-tauri\tauri.conf.json'; Path = $tauriConf;  Kind = 'json'  },
    @{ Label = '#source\frontend\package.json';         Path = $frontendPkg; Kind = 'json'  },
    @{ Label = '#source\app\package.json';              Path = $appPkg;      Kind = 'json'  }
)

function Get-VersionMismatches([string]$expected) {
    $bad = New-Object System.Collections.ArrayList
    $actualVersionFile = ''
    try { $actualVersionFile = ([System.IO.File]::ReadAllText($versionFile)).Trim() } catch { $actualVersionFile = '<读取失败>' }
    if ($actualVersionFile -ne $expected) { [void]$bad.Add(("#source\version.txt = {0}" -f $actualVersionFile)) }
    foreach ($t in $versionTargets) {
        try {
            $v = (Find-VersionField $t.Path $t.Kind).Value
        } catch {
            [void]$bad.Add(("{0} = <读取失败：{1}>" -f $t.Label, $_.Exception.Message))
            continue
        }
        if ($v -ne $expected) { [void]$bad.Add(("{0} = {1}" -f $t.Label, $v)) }
    }
    return $bad
}

function Assert-VersionConsistency([string]$expected) {
    $bad = @(Get-VersionMismatches $expected)
    if ($bad.Count -gt 0) {
        $lines = $bad -join ($NL + '    ')
        throw ("版本不一致：真源 #source\version.txt = {0}，以下 {1} 处不符：" -f $expected, $bad.Count) + ($NL + $NL) + '    ' + $lines + ($NL + $NL) + ("  修法：pwsh -NoProfile -File #source\scripts\build-release.ps1 -BumpVersion {0}" -f $expected)
    }
    Write-Host ("    版本 5 处一致 = {0}" -f $expected) -ForegroundColor DarkGray
}

function Invoke-BumpVersion([string]$target) {
    if ($target -notmatch '^\d+\.\d+\.\d+$') { throw ("-BumpVersion 需要 x.y.z 形式，收到：'{0}'" -f $target) }
    $before = '<缺失>'
    try { $before = ([System.IO.File]::ReadAllText($versionFile)).Trim() } catch { }
    if ($before -eq $target) {
        Write-Host ("版本统一：真源已是 {0}，把其余 4 处对齐到它" -f $target)
    } else {
        Write-Host ("版本改写：{0} -> {1}" -f $before, $target)
    }
    Write-FileText $versionFile ($target + $NL) $false
    Write-Host ("    #source\version.txt = {0}" -f $target) -ForegroundColor DarkGray
    foreach ($t in $versionTargets) {
        $info = Find-VersionField $t.Path $t.Kind
        $newText = $info.Text.Substring(0, $info.Offset) + $info.Prefix + $target + $info.Suffix +
                   $info.Text.Substring($info.Offset + $info.Length)
        Write-FileText $t.Path $newText $info.Bom
        Write-Host ("    {0} = {1}（原 {2}）" -f $t.Label, $target, $info.Value) -ForegroundColor DarkGray
    }
    Assert-VersionConsistency $target
    Write-Host ""
    Write-Host ("版本号已统一为 {0}（5 处）。未执行构建。" -f $target) -ForegroundColor Green
}

# ---- 模型必需文件白名单（预检与 Verify 共用同一套断言） ------------------------
# 只断言推理真正加载的文件（fireredasr2s 各 from_pretrained；runtime.py 选 vad\VAD
# 子目录）。「目录非空」抓不到半截下载/损坏的权重，必须按文件 + 最小字节数断言。
function Get-ModelRequirements {
    return @{
        aed  = @(
            @{ Rel = 'model.pth.tar';         MinBytes = 4GB },
            @{ Rel = 'cmvn.ark';              MinBytes = 1KB },
            @{ Rel = 'dict.txt';              MinBytes = 10KB },
            @{ Rel = 'train_bpe1000.model';   MinBytes = 100KB }
        )
        vad  = @(
            @{ Rel = 'VAD\cmvn.ark';          MinBytes = 1KB },
            @{ Rel = 'VAD\model.pth.tar';     MinBytes = 1MB }
        )
        punc = @(
            @{ Rel = 'model.pth.tar';                       MinBytes = 300MB },
            @{ Rel = 'chinese-bert-wwm-ext_vocab.txt';      MinBytes = 50KB },
            @{ Rel = 'out_dict';                            MinBytes = 16 },
            @{ Rel = 'chinese-lert-base\config.json';       MinBytes = 100 },
            @{ Rel = 'chinese-lert-base\pytorch_model.bin'; MinBytes = 300MB },
            @{ Rel = 'chinese-lert-base\vocab.txt';         MinBytes = 50KB },
            @{ Rel = 'chinese-lert-base\tokenizer.json';    MinBytes = 100KB }
        )
    }
}

function Get-ModelFileProblems([string]$modelDir, [string]$model) {
    $problems = New-Object System.Collections.ArrayList
    foreach ($f in (Get-ModelRequirements)[$model]) {
        $p = Join-Path $modelDir $f.Rel
        if (-not (Test-Path -LiteralPath $p)) {
            [void]$problems.Add("缺必需文件：$($f.Rel)")
            continue
        }
        $len = (Get-Item -LiteralPath $p).Length
        if ($len -lt $f.MinBytes) {
            [void]$problems.Add(("{0} 字节数不足：{1:n0} < {2:n0}" -f $f.Rel, $len, $f.MinBytes))
        }
    }
    return $problems
}

function Get-ModelProblems([string]$modelsRoot) {
    $problems = New-Object System.Collections.ArrayList
    foreach ($model in @('aed', 'vad', 'punc')) {
        $dir = Join-Path $modelsRoot $model
        if (-not (Test-Path -LiteralPath $dir)) {
            [void]$problems.Add("模型目录缺失：$dir")
            continue
        }
        foreach ($p in @(Get-ModelFileProblems $dir $model)) {
            [void]$problems.Add(("models\{0} {1}" -f $model, $p))
        }
    }
    return $problems
}

# ---- Python ABI（便携 runtime 必须 3.11，且与开发 venv 一致） ------------------
function Get-PythonMajorMinor([string]$pythonExe) {
    $out = (& $pythonExe -c "import sys;print('{0}.{1}'.format(*sys.version_info[:2]))" 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) { throw "无法读取 Python 版本（exit $LASTEXITCODE）：$pythonExe -> $out" }
    if ($out -notmatch '^\d+\.\d+$') { throw "Python 版本输出异常：$pythonExe -> '$out'" }
    return $out
}

function Get-VenvPythonVersion {
    $cfg = Join-Path $engineDir '.venv\pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $cfg)) { throw "缺少开发 venv 配置：$cfg" }
    foreach ($line in [System.IO.File]::ReadAllLines($cfg)) {
        if ($line -match '^\s*(?:version_info|version)\s*=\s*(\d+)\.(\d+)') {
            return ($Matches[1] + '.' + $Matches[2])
        }
    }
    throw "pyvenv.cfg 没有 version_info/version 字段：$cfg"
}

# ---- 预检（拷贝前一次查清，不拷完 10GB 才报错） ------------------------------
function Get-PreflightProblems {
    $missing = New-Object System.Collections.ArrayList

    if (-not $SkipFrontend) {
        if (-not (Test-Path -LiteralPath (Join-Path $frontend 'package.json'))) {
            [void]$missing.Add("前端工程：$frontend\package.json")
        }
        if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
            [void]$missing.Add('pnpm（前端构建需要，检查 PATH）')
        }
    }
    if (-not $SkipCargo) {
        if (-not (Test-Path -LiteralPath $cargoToml)) { [void]$missing.Add("Tauri 工程：$cargoToml") }
        if (-not (Get-Command cargo.exe -ErrorAction SilentlyContinue)) {
            [void]$missing.Add('cargo（Rust 工具链，检查 PATH 或 ..\_tools\cargo\bin）')
        }
    }

    if (-not $SkipPayload) {
        $checks = @(
            @{ Path = (Join-Path $ffmpegDirResolved 'ffmpeg.exe');  What = ("ffmpeg.exe（-FfmpegDir，当前：{0}）" -f $ffmpegDirResolved) },
            @{ Path = (Join-Path $ffmpegDirResolved 'ffprobe.exe'); What = ("ffprobe.exe（-FfmpegDir，当前：{0}）" -f $ffmpegDirResolved) },
            @{ Path = (Join-Path $pythonRuntimeResolved 'python.exe'); What = ("便携 CPython（-PythonRuntime，当前：{0}）" -f $pythonRuntimeResolved) },
            @{ Path = (Join-Path $venvSitePackages 'torch');        What = ("site-packages\torch（{0}）" -f $venvSitePackages) },
            @{ Path = (Join-Path $engineDir 'voicetrace_engine');   What = ("引擎源码：{0}" -f (Join-Path $engineDir 'voicetrace_engine')) },
            @{ Path = (Join-Path $engineDir 'third_party\FireRedASR2S'); What = ("FireRedASR2S 源码：{0}" -f (Join-Path $engineDir 'third_party\FireRedASR2S')) }
        )
        foreach ($c in $checks) {
            if (-not (Test-Path -LiteralPath $c.Path)) { [void]$missing.Add($c.What) }
        }
        if (-not (Test-Path -LiteralPath $venvSitePackages)) { [void]$missing.Add("venv site-packages：$venvSitePackages") }
        # 模型按必需文件白名单 + 最小字节数断言（与 Verify 同一函数）。
        foreach ($p in @(Get-ModelProblems (Join-Path $engineDir 'models'))) { [void]$missing.Add($p) }
        # 便携 Python ABI：必须 3.11（torch cp311 wheel），且与开发 venv 一致。
        $portablePyExe = Join-Path $pythonRuntimeResolved 'python.exe'
        if (Test-Path -LiteralPath $portablePyExe) {
            try {
                $pv = Get-PythonMajorMinor $portablePyExe
                if ($pv -ne '3.11') { [void]$missing.Add("便携 Python ABI 不符：$pv（必须 3.11，与 torch cp311 wheel 匹配）") }
                $vv = Get-VenvPythonVersion
                if ($pv -ne $vv) { [void]$missing.Add("便携 Python（$pv）与开发 venv（$vv）ABI 不一致") }
            } catch {
                [void]$missing.Add($_.Exception.Message)
            }
        }
        if (-not (Get-Command robocopy.exe -ErrorAction SilentlyContinue)) { [void]$missing.Add('robocopy.exe（Windows 自带）') }
        if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) { [void]$missing.Add('node.exe（递归删除 payload 目录用）') }
    }
    return $missing
}

function Assert-Preflight {
    $problems = @(Get-PreflightProblems)
    if ($problems.Count -gt 0) {
        throw ("拷贝前预检未通过（{0} 项），未复制任何文件：" -f $problems.Count) + ($NL + $NL) + '  - ' + ($problems -join ($NL + '  - '))
    }
    Write-Host "    预检通过：ffmpeg/ffprobe、Python 运行时（ABI=3.11 且与 venv 一致）、models\{aed,vad,punc} 必需文件、site-packages\torch、引擎源码均达标" -ForegroundColor DarkGray
}

# ---- 校验 --------------------------------------------------------------------
$script:checks = $null

function Add-Check([string]$name, [bool]$pass, [string]$detail) {
    [void]$script:checks.Add([pscustomobject]@{ Name = $name; Pass = $pass; Detail = $detail })
    if ($pass) {
        Write-Host ("    [PASS] " + $name) -ForegroundColor DarkGray
        if ($detail) { Write-Host ("           " + $detail) -ForegroundColor DarkGray }
    } else {
        Write-Host ("    [FAIL] " + $name + "  " + $detail) -ForegroundColor Red
    }
}

function Test-ExeVersionValue([string]$exePath, [string]$expected) {
    try {
        $vi = (Get-Item -LiteralPath $exePath).VersionInfo
        $fv = $vi.FileVersion
        if (-not $fv) { return @{ Ok = $false; Detail = 'exe 没有版本资源' } }
        $parts = @($fv.Trim() -split '\.' | Select-Object -First 3)
        $norm = $parts -join '.'
        if ($norm -ne $expected) { return @{ Ok = $false; Detail = ("FileVersion={0}，期望 {1}" -f $fv, $expected) } }
        return @{ Ok = $true; Detail = ("FileVersion={0}" -f $fv) }
    } catch {
        return @{ Ok = $false; Detail = $_.Exception.Message }
    }
}

function Get-PackageFileList([string]$releaseDir) {
    $result = New-Object System.Collections.ArrayList
    if (-not (Test-Path -LiteralPath $releaseDir)) { return $result }
    $stack = New-Object System.Collections.Stack
    $stack.Push($releaseDir.TrimEnd('\'))
    while ($stack.Count -gt 0) {
        $dir = $stack.Pop()
        foreach ($f in @(Get-ChildItem -LiteralPath $dir -File -Force -ErrorAction SilentlyContinue)) {
            [void]$result.Add($f)
        }
        foreach ($d in @(Get-ChildItem -LiteralPath $dir -Directory -Force -ErrorAction SilentlyContinue)) {
            if ($d.Name -in @('__pycache__', '.git', '.pytest_cache', '.mypy_cache')) { continue }
            if ($dir -eq $releaseDir.TrimEnd('\') -and $d.Name -eq 'data') { continue }
            $stack.Push($d.FullName)
        }
    }
    return $result
}

function Write-Checksums([string]$releaseDir) {
    $sumsPath = Join-Path $releaseDir 'SHA256SUMS.txt'
    $files = @(Get-PackageFileList $releaseDir | Where-Object { $_.FullName -ne $sumsPath } | Sort-Object FullName)
    if ($files.Count -eq 0) { throw "便携包里没有文件，拒绝生成 SHA256SUMS.txt：$releaseDir" }
    $prefix = $releaseDir.TrimEnd('\').Length
    $sb = New-Object System.Text.StringBuilder
    $n = 0
    foreach ($f in $files) {
        $rel = $f.FullName.Substring($prefix).TrimStart('\') -replace '\\', '/'
        $hash = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        [void]$sb.AppendLine(('{0}  {1}' -f $hash, $rel))
        $n++
    }
    [System.IO.File]::WriteAllText($sumsPath, $sb.ToString(), (New-Object System.Text.UTF8Encoding($false)))
    return @{ Path = $sumsPath; Count = $n }
}

function Test-Checksums([string]$releaseDir) {
    $sumsPath = Join-Path $releaseDir 'SHA256SUMS.txt'
    if (-not (Test-Path -LiteralPath $sumsPath)) { return @{ Ok = $false; Detail = ("缺少 SHA256SUMS.txt：{0}" -f $sumsPath) } }
    $lines = [System.IO.File]::ReadAllLines($sumsPath)
    $checked = 0
    $missing = @()
    $bad = @()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})\s+\*?(.+)$') {
            return @{ Ok = $false; Detail = ("SHA256SUMS.txt 行格式非法：{0}" -f $line) }
        }
        $expectedHash = $Matches[1].ToLowerInvariant()
        $rel = ($Matches[2].Trim()) -replace '/', '\'
        $path = Join-Path $releaseDir $rel
        if (-not (Test-Path -LiteralPath $path)) { $missing += $rel; continue }
        $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expectedHash) { $bad += $rel }
        $checked++
    }
    if ($missing.Count -gt 0) {
        return @{ Ok = $false; Detail = ("清单中 {0} 个文件不存在，例如：{1}" -f $missing.Count, (($missing | Select-Object -First 3) -join '、')) }
    }
    if ($bad.Count -gt 0) {
        return @{ Ok = $false; Detail = ("{0} 个文件哈希不符，例如：{1}" -f $bad.Count, (($bad | Select-Object -First 3) -join '、')) }
    }
    return @{ Ok = $true; Detail = ("{0} 个文件逐文件 SHA256 一致" -f $checked) }
}

function Invoke-Verify([string]$releaseDir, [string]$expectedVersion) {
    $script:checks = New-Object System.Collections.ArrayList
    Write-Host ""
    Write-Host ("校验目标：{0}" -f $releaseDir) -ForegroundColor Yellow

    # 1) 版本 5 处一致
    if (-not $expectedVersion) {
        Add-Check '版本 5 处一致' $false '版本真源 #source\version.txt 缺失或格式非法'
    } else {
        $bad = @(Get-VersionMismatches $expectedVersion)
        $detail = ''
        if ($bad.Count -gt 0) { $detail = '不符：' + ($bad -join '；') }
        Add-Check ("版本 5 处一致 = {0}" -f $expectedVersion) ($bad.Count -eq 0) $detail
    }

    # 2) exe 与版本资源
    $exePath = Join-Path $releaseDir $exeName
    if (Test-Path -LiteralPath $exePath) {
        Add-Check '声迹.exe 存在' $true ("{0:n1} MB" -f ((Get-Item -LiteralPath $exePath).Length / 1MB))
        if ($expectedVersion) {
            $r = Test-ExeVersionValue $exePath $expectedVersion
            Add-Check ("声迹.exe 版本资源 = {0}" -f $expectedVersion) $r.Ok $r.Detail
        } else {
            Add-Check '声迹.exe 版本资源' $false '无法比对：版本真源不可用'
        }
    } else {
        Add-Check '声迹.exe 存在' $false $exePath
        Add-Check '声迹.exe 版本资源' $false 'exe 不存在'
    }

    # 3) WebView2Loader.dll
    $dll = Join-Path $releaseDir 'WebView2Loader.dll'
    Add-Check 'WebView2Loader.dll 存在' (Test-Path -LiteralPath $dll) $dll

    # 4) 使用说明.txt 含版本号
    $readme = Join-Path $releaseDir '使用说明.txt'
    if (Test-Path -LiteralPath $readme) {
        $text = [System.IO.File]::ReadAllText($readme)
        $hasVersion = $false
        if ($expectedVersion) { $hasVersion = $text -like ("*" + $expectedVersion + "*") }
        $detail = ''
        if (-not $hasVersion) { $detail = ("未包含版本号 {0}" -f $expectedVersion) }
        Add-Check '使用说明.txt 存在且写有当前版本号' ([bool]$hasVersion) $detail
    } else {
        Add-Check '使用说明.txt 存在且写有当前版本号' $false $readme
    }

    # 5) engine 源码
    $engineTarget = Join-Path $releaseDir 'engine\voicetrace_engine'
    if (Test-Path -LiteralPath $engineTarget) {
        $py = @(Get-ChildItem -LiteralPath $engineTarget -Recurse -File -Filter '*.py' -Force -ErrorAction SilentlyContinue)
        Add-Check 'engine\voicetrace_engine 的 .py 数量 > 0' ($py.Count -gt 0) ("{0} 个 .py 文件" -f $py.Count)
    } else {
        Add-Check 'engine\voicetrace_engine 的 .py 数量 > 0' $false $engineTarget
    }

    # 5b) 引擎读版本用的 engine\version.txt（缺失会让 /health 回落到兜底值）
    $engineVersion = Join-Path $releaseDir 'engine\version.txt'
    if (Test-Path -LiteralPath $engineVersion) {
        $got = (Get-Content -LiteralPath $engineVersion -Raw).Trim()
        Add-Check ("engine\version.txt = {0}" -f $expectedVersion) ($got -eq $expectedVersion) $got
    } else {
        Add-Check ("engine\version.txt = {0}" -f $expectedVersion) $false '缺失（引擎会回落兜底版本号）'
    }

    # 6) third_party 源码
    $fireInit = Join-Path $releaseDir 'engine\third_party\FireRedASR2S\fireredasr2s\__init__.py'
    Add-Check 'engine\third_party\FireRedASR2S\fireredasr2s\__init__.py' (Test-Path -LiteralPath $fireInit) $fireInit

    # 7) models 三件套（必需文件白名单 + 最小字节数，与预检同一函数）
    foreach ($model in @('aed', 'vad', 'punc')) {
        $dir = Join-Path $releaseDir "models\$model"
        $name = ("models\{0} 必需文件齐全（白名单+最小字节数）" -f $model)
        if (-not (Test-Path -LiteralPath $dir)) {
            Add-Check $name $false $dir
            continue
        }
        $probs = @(Get-ModelFileProblems $dir $model)
        Add-Check $name ($probs.Count -eq 0) ($probs -join '；')
    }

    # 8) punc 无 tf_model.h5
    $puncDir = Join-Path $releaseDir 'models\punc'
    if (Test-Path -LiteralPath $puncDir) {
        $tf = @(Get-ChildItem -LiteralPath $puncDir -Recurse -File -Force -Filter 'tf_model.h5' -ErrorAction SilentlyContinue)
        $detail = ''
        if ($tf.Count -gt 0) { $detail = ("不应包含：{0}" -f $tf[0].FullName) }
        Add-Check 'models\punc 无 tf_model.h5（省 454 MB）' ($tf.Count -eq 0) $detail
    } else {
        Add-Check 'models\punc 无 tf_model.h5（省 454 MB）' $false '模型目录不存在'
    }

    # 9) runtime\python 可运行，且 ABI = 3.11、与开发 venv 一致（torch cp311 wheel）
    $portablePython = Join-Path $releaseDir 'runtime\python\python.exe'
    $abiVersion = ''
    if (Test-Path -LiteralPath $portablePython) {
        $verOut = ''
        $ok = $false
        try {
            $verOut = (& $portablePython --version 2>&1 | Out-String).Trim()
            $ok = ($LASTEXITCODE -eq 0) -and ($verOut -match 'Python 3\.\d+')
        } catch {
            $ok = $false
            $verOut = $_.Exception.Message
        }
        Add-Check 'runtime\python\python.exe --version 可跑' $ok $verOut
        try {
            $abiVersion = Get-PythonMajorMinor $portablePython
            Add-Check 'runtime\python ABI = 3.11' ($abiVersion -eq '3.11') ("sys.version_info[:2] = {0}" -f $abiVersion)
        } catch {
            Add-Check 'runtime\python ABI = 3.11' $false $_.Exception.Message
        }
        try {
            $venvVersion = Get-VenvPythonVersion
            $same = ($abiVersion -ne '') -and ($abiVersion -eq $venvVersion)
            Add-Check '便携 Python 与开发 venv ABI 一致' $same ("便携={0}，venv={1}" -f $abiVersion, $venvVersion)
        } catch {
            Add-Check '便携 Python 与开发 venv ABI 一致' $false $_.Exception.Message
        }
    } else {
        Add-Check 'runtime\python\python.exe --version 可跑' $false $portablePython
        Add-Check 'runtime\python ABI = 3.11' $false $portablePython
        Add-Check '便携 Python 与开发 venv ABI 一致' $false $portablePython
    }

    # 10) site-packages\torch
    $torchDir = Join-Path $releaseDir 'runtime\site-packages\torch'
    Add-Check 'runtime\site-packages\torch 存在' (Test-Path -LiteralPath $torchDir) $torchDir

    # 11) 便携 Python 能解析 torch（PYTHONPATH=site-packages）
    if ((Test-Path -LiteralPath $portablePython) -and (Test-Path -LiteralPath $torchDir)) {
        $site = Join-Path $releaseDir 'runtime\site-packages'
        $oldPythonPath = $env:PYTHONPATH
        $env:PYTHONPATH = $site
        $ok = $false
        $probe = ''
        try {
            $probe = (& $portablePython -c "import importlib.util,sys;sys.exit(0 if importlib.util.find_spec('torch') else 3)" 2>&1 | Out-String).Trim()
            $ok = ($LASTEXITCODE -eq 0)
        } catch {
            $ok = $false
            $probe = $_.Exception.Message
        } finally {
            $env:PYTHONPATH = $oldPythonPath
        }
        Add-Check '便携 Python 能解析 torch（find_spec）' $ok $probe
    } else {
        Add-Check '便携 Python 能解析 torch（find_spec）' $false 'python.exe 或 torch 目录缺失'
    }

    # 12) ffmpeg / ffprobe
    $ffTargets = @(
        @{ Path = (Join-Path $releaseDir 'runtime\ffmpeg.exe');  Name = 'runtime\ffmpeg.exe 存在' },
        @{ Path = (Join-Path $releaseDir 'runtime\ffprobe.exe'); Name = 'runtime\ffprobe.exe 存在' }
    )
    foreach ($x in $ffTargets) { Add-Check $x.Name (Test-Path -LiteralPath $x.Path) $x.Path }

    # 13) SHA256SUMS 全量校验
    $sumsResult = Test-Checksums $releaseDir
    Add-Check 'SHA256SUMS.txt 存在且逐文件哈希一致' $sumsResult.Ok $sumsResult.Detail

    # 14) 包里不能有运行期 data\
    $dataDir = Join-Path $releaseDir 'data'
    $hasData = Test-Path -LiteralPath $dataDir
    $detail = ''
    if ($hasData) { $detail = ("运行期数据被打进包：{0}" -f $dataDir) }
    Add-Check '包内不含运行期 data\' (-not $hasData) $detail

    # 15) 包内无 __pycache__ / .git
    $junk = @()
    $engineOut = Join-Path $releaseDir 'engine'
    if (Test-Path -LiteralPath $engineOut) {
        $junk = @(Get-ChildItem -LiteralPath $engineOut -Recurse -Directory -Force -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -in @('__pycache__', '.git', '.pytest_cache') })
    }
    $detail = ''
    if ($junk.Count -gt 0) { $detail = ("{0} 个，例如 {1}" -f $junk.Count, $junk[0].FullName) }
    Add-Check 'engine 内无 __pycache__ / .git / .pytest_cache' ($junk.Count -eq 0) $detail

    $failed = @($script:checks | Where-Object { -not $_.Pass })
    Write-Host ""
    if ($failed.Count -eq 0) {
        Write-Host ("校验通过：{0} 项全部达标" -f $script:checks.Count) -ForegroundColor Green
    } else {
        Write-Host ("校验未通过：{0} 项失败 / 共 {1} 项" -f $failed.Count, $script:checks.Count) -ForegroundColor Red
        $detailLines = ($failed | ForEach-Object { $_.Name + '：' + $_.Detail }) -join ($NL + '  - ')
        throw ("-Verify 未通过（{0} 项失败）：" -f $failed.Count) + ($NL + $NL) + '  - ' + $detailLines
    }
}

# ---- 使用说明.txt -------------------------------------------------------------
function Get-UsageText([string]$version) {
    $template = @'
声迹 VoiceTrace — 便携版 @VERSION@
==================================

一、启动
  双击「声迹.exe」。首次启动需要加载本地模型，通常 20–90 秒（显卡较弱或杀毒在扫描时更久），
  启动页会显示进度；失败时会显示引擎日志的最后 50 行。
  提示：本程序是免安装的便携版，请先把整个目录解压/复制到本地磁盘再运行（不要直接在压缩包里双击）。

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
  - 默认数据目录：本目录下的 data（数据库、字幕、设置、日志、WebView 缓存）。
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
  models\{aed,vad,punc}\          本地模型
  runtime\python\                 独立 CPython 3.11
  runtime\site-packages\          Python 依赖（含 torch）
  runtime\ffmpeg.exe              音视频处理
  SHA256SUMS.txt                  全量文件校验和（可逐行核对）
  使用说明.txt                    本文件

五、注意
  - 不要单独移动 exe；engine、models、runtime 必须与它同级，并保持内部目录结构不变。
  - 目录可以放在含中文或空格的路径下；不建议放进会被同步/虚拟化的目录（如 OneDrive）。
  - 首次运行若被 SmartScreen 拦截，选择「更多信息 → 仍要运行」。
  - 若杀毒软件误报，请把整个目录加入信任（便携 Python 与 torch 的动态库常被误判）。
  - 同一时间只运行一个实例（见六）。
  - 本版本免安装，删除整个目录即可卸载（数据目录需手动删除）。

六、常见故障
  1) 启动即报错，错误码 0x800700AA（请求的资源在使用中）：
     多为同时开了两个实例，或上一实例尚未完全退出，WebView2 用户数据目录被占用。
     处理：在任务管理器结束所有「声迹.exe」与本目录下的 python.exe，再重新启动。
  2) 一直停在启动页、超过 90 秒：
     首次加载模型本身就慢（模型约 5.8 GB，torch 首次初始化要建 CUDA 上下文），先等满 2 分钟；
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
'@
    return $template.Replace('@VERSION@', $version)
}

# ============================== 主流程 ========================================
try {
    Write-Host "声迹 VoiceTrace 便携版打包" -ForegroundColor Yellow
    Write-Host "项目根：$root"
    Write-Host "输出：  $release"

    if ($BumpVersion) {
        Invoke-BumpVersion $BumpVersion
        exit 0
    }

    if ($Verify) {
        $expected = ''
        try { $expected = Get-SourceVersion } catch { $expected = '' }
        Write-Host "模式：-Verify 只校验（不构建、不拷贝、不写盘）" -ForegroundColor DarkYellow
        Invoke-Verify $release $expected
        exit 0
    }

    if ($SkipFrontend -and $SkipCargo -and $SkipPayload) {
        Write-Host ""
        Write-Host "未产出可交付包：-SkipFrontend -SkipCargo -SkipPayload 三项同时给出时，" -ForegroundColor Red
        Write-Host "本脚本不做任何构建、不复制任何文件、也不创建输出目录（这不是 dry-run，产物为 0）。" -ForegroundColor Red
        Write-Host "  只检查脚本语法：去掉至少一个 -Skip（例如只给 -SkipPayload），或改用 -Verify 校验现有产物。" -ForegroundColor Yellow
        Write-Host "  退出码 2。" -ForegroundColor Yellow
        exit 2
    }

    $version = Get-SourceVersion
    Write-Host ("版本：  {0}" -f $version)

    Step "版本一致性断言（5 处 = #source\version.txt）" {
        Assert-VersionConsistency $version
    }

    Step "拷贝前预检（ffmpeg / Python / models / site-packages）" {
        Assert-Preflight
    }

    if (-not $SkipFrontend) {
        Step "构建前端（$frontend）" {
            Assert-File (Join-Path $frontend 'package.json') '前端 package.json'
            Push-Location $frontend
            try {
                # SPEC §6：每次构建都对齐 lockfile，不能只在 node_modules 缺失时才装。
                Write-Host "    pnpm install --frozen-lockfile"
                pnpm install --frozen-lockfile
                if ($LASTEXITCODE -ne 0) { throw "pnpm install 失败（exit $LASTEXITCODE）" }
                pnpm build
                if ($LASTEXITCODE -ne 0) { throw "前端 pnpm build 失败（exit $LASTEXITCODE）" }
            } finally {
                Pop-Location
            }
            Assert-File (Join-Path $frontend 'dist\index.html') '前端产物 dist/index.html'
        }
    } else {
        Write-Host ""
        Write-Host "[跳过] 前端构建（-SkipFrontend，复用已有 dist）" -ForegroundColor DarkYellow
        Assert-File (Join-Path $frontend 'dist\index.html') '前端产物 dist/index.html（-SkipFrontend 时须已存在）'
    }

    if (-not $SkipCargo) {
        Step "编译 Rust release（cargo build --release --features custom-protocol）" {
            Assert-File $cargoToml 'src-tauri/Cargo.toml'
            Push-Location $srcTauri
            try {
                cargo build --release --features custom-protocol
                if ($LASTEXITCODE -ne 0) { throw "cargo build --release --features custom-protocol 失败（exit $LASTEXITCODE）" }
            } finally {
                Pop-Location
            }
            Assert-File $exeSource 'release 可执行文件'
        }
    } else {
        Write-Host ""
        Write-Host "[跳过] cargo build（-SkipCargo，复用已有 release exe）" -ForegroundColor DarkYellow
    }

    Step "准备输出目录并复制 exe" {
        New-Item -ItemType Directory -Force -Path $release | Out-Null
        Assert-File $exeSource 'release 可执行文件（先去掉 -SkipCargo）'
        Copy-Item -LiteralPath $exeSource -Destination (Join-Path $release $exeName) -Force
        Write-Host ("    {0}  {1:n1} MB" -f $exeName, ((Get-Item -LiteralPath (Join-Path $release $exeName)).Length / 1MB))
        # GNU 工具链下 WebView2 是动态加载的，必须和 exe 同目录，否则启动即 0xc0000135。
        Assert-File $webviewDllSource 'WebView2Loader.dll（GNU 构建产物，需与 exe 同级）'
        Copy-Item -LiteralPath $webviewDllSource -Destination (Join-Path $release 'WebView2Loader.dll') -Force
        Write-Host ("    WebView2Loader.dll  {0:n1} KB" -f ((Get-Item -LiteralPath (Join-Path $release 'WebView2Loader.dll')).Length / 1KB))
    }

    if (-not $SkipPayload) {
        Step "清空目标端 payload（幂等：engine / models / runtime / data）" {
            foreach ($name in @('engine', 'models', 'runtime')) {
                Remove-Tree (Join-Path $release $name)
            }
            # data 是运行期产物（数据库/字幕/日志/WebView 缓存），必须清掉，绝不能进包。
            # 删除前先整目录备份到 _archive（里面可能有用户转录）；备份失败即中止删除。
            $dataDir = Join-Path $release 'data'
            if (Test-Path -LiteralPath $dataDir) {
                $backupRoot = Join-Path $root ('_archive\{0}-release-data' -f (Get-Date -Format 'yyyy-MM-dd'))
                $backupDir = Join-Path $backupRoot (Get-Date -Format 'HHmmss')
                if (Test-Path -LiteralPath $backupDir) {
                    $backupDir = $backupDir + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
                }
                Write-Host ("    发现运行期 data\（绝不进包），先备份到 {0} …" -f $backupDir) -ForegroundColor DarkYellow
                New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
                $out = & robocopy.exe $dataDir $backupDir /E /MT:8 /R:2 /W:2 /NP /NFL /NDL /NJH 2>&1
                $code = $LASTEXITCODE
                if ($code -ge 4) {
                    $detail = (@($out | ForEach-Object { [string]$_ }) | Select-Object -Last 20 | Out-String)
                    throw ("data 备份失败（robocopy exit {0}），中止删除以保护用户数据：{1} -> {2}" -f $code, $dataDir, $backupDir) + $NL + $detail
                }
                $srcCount = @(Get-ChildItem -LiteralPath $dataDir -Recurse -File -Force -ErrorAction SilentlyContinue).Count
                $dstCount = @(Get-ChildItem -LiteralPath $backupDir -Recurse -File -Force -ErrorAction SilentlyContinue).Count
                if ($dstCount -lt $srcCount) {
                    throw ("data 备份不完整（{0}/{1} 个文件），中止删除：{2}" -f $dstCount, $srcCount, $backupDir)
                }
                $script:dataBackupDir = $backupDir
                Write-Host ("    备份完成（{0} 个文件，robocopy exit {1}），现在清除包内 data\" -f $dstCount, $code) -ForegroundColor DarkGray
                Remove-Tree $dataDir
            }
            Get-ChildItem -LiteralPath $release -File -Force -ErrorAction SilentlyContinue |
                Where-Object { $_.Name -in @('SHA256SUMS.txt', '使用说明.txt') } |
                ForEach-Object {
                    Remove-Item -LiteralPath $_.FullName -Force
                    Write-Host ("    已删除旧 {0}" -f $_.Name) -ForegroundColor DarkGray
                }
        }

        Step "复制 Python 引擎包 engine\voicetrace_engine" {
            $src = Join-Path $engineDir 'voicetrace_engine'
            Assert-File $src '引擎包（voicetrace_engine/ 不存在）'
            Invoke-Robocopy $src (Join-Path $release 'engine\voicetrace_engine') 'engine/voicetrace_engine'
        }

        # 引擎按 config._read_version() 的第二个候选路径找版本：<release>\engine\version.txt。
        # 少了这个文件，包里引擎会回落到兜底值（1.3.1 的 e2e 就是靠 /health 报 1.3.0 抓到的）。
        Step "写入 engine\version.txt（引擎读版本用）" {
            $versionSrc = Join-Path $root '#source\version.txt'
            Assert-File $versionSrc '版本真源 #source\version.txt'
            Copy-Item -LiteralPath $versionSrc -Destination (Join-Path $release 'engine\version.txt') -Force
            $shipped = (Get-Content -LiteralPath $versionSrc -Raw).Trim()
            Write-Host ("    engine\version.txt = {0}" -f $shipped) -ForegroundColor DarkGray
        }

        Step "复制 third_party\FireRedASR2S（便携转录必需）" {
            $src = Join-Path $engineDir 'third_party\FireRedASR2S'
            Assert-File $src 'third_party\FireRedASR2S'
            Invoke-Robocopy $src (Join-Path $release 'engine\third_party\FireRedASR2S') 'engine/third_party/FireRedASR2S'
        }

        Step "复制模型 models\{aed,vad,punc}" {
            foreach ($model in @('aed', 'vad', 'punc')) {
                $src = Join-Path $engineDir "models\$model"
                Assert-File $src "模型 $model"
                Invoke-Robocopy $src (Join-Path $release "models\$model") "models/$model"
            }
            # /XF 只阻止复制；万一目标端已有旧版 tf_model.h5，这里显式确认。
            $tf = Join-Path $release 'models\punc\chinese-lert-base\tf_model.h5'
            if (Test-Path -LiteralPath $tf) {
                Remove-Item -LiteralPath $tf -Force
                Write-Host '    已删除旧包中的 tf_model.h5（省 454 MB）'
            }
        }

        Step "复制独立 Python 运行时 runtime\python" {
            Assert-File (Join-Path $pythonRuntimeResolved 'python.exe') 'Python 运行时'
            Invoke-Robocopy $pythonRuntimeResolved (Join-Path $release 'runtime\python') 'runtime/python'
        }

        Step "复制 site-packages（大目录，支持断点重跑）" {
            Assert-File $venvSitePackages 'engine/.venv/Lib/site-packages'
            $items = @(Get-ChildItem -LiteralPath $venvSitePackages -Force -ErrorAction Stop)
            if ($items.Count -eq 0) {
                throw "site-packages 为空：$venvSitePackages（引擎依赖可能尚未安装完成）"
            }
            Invoke-Robocopy $venvSitePackages (Join-Path $release 'runtime\site-packages') 'runtime/site-packages'
        }

        Step "复制 ffmpeg.exe / ffprobe.exe" {
            Assert-File (Join-Path $ffmpegDirResolved 'ffmpeg.exe') 'ffmpeg.exe'
            Assert-File (Join-Path $ffmpegDirResolved 'ffprobe.exe') 'ffprobe.exe'
            New-Item -ItemType Directory -Force -Path (Join-Path $release 'runtime') | Out-Null
            Copy-Item -LiteralPath (Join-Path $ffmpegDirResolved 'ffmpeg.exe') -Destination (Join-Path $release 'runtime\ffmpeg.exe') -Force
            Copy-Item -LiteralPath (Join-Path $ffmpegDirResolved 'ffprobe.exe') -Destination (Join-Path $release 'runtime\ffprobe.exe') -Force
        }
    } else {
        Write-Host ""
        Write-Host "[跳过] 引擎/模型/运行时复制（-SkipPayload）：这不是完整便携包" -ForegroundColor DarkYellow
    }

    if (-not $SkipPayload) {
        Step "写入 使用说明.txt" {
            New-Item -ItemType Directory -Force -Path $release | Out-Null
            $readmePath = Join-Path $release '使用说明.txt'
            $text = (Get-UsageText $version) -split "\r?\n"
            $joined = [string]::Join($CRLF, $text)
            [System.IO.File]::WriteAllText($readmePath, $joined, (New-Object System.Text.UTF8Encoding($true)))
            Write-Host ("    使用说明.txt（版本 {0}，含四份日志路径与常见故障）" -f $version)
        }
    } else {
        Write-Host ""
        Write-Host "[跳过] 重写 使用说明.txt（-SkipPayload：保留旧文件，避免与旧 SHA256SUMS.txt 哈希不符）" -ForegroundColor DarkYellow
    }

    if (-not $SkipPayload) {
        Step "生成 SHA256SUMS.txt（全量逐文件哈希）" {
            $sums = Write-Checksums $release
            Write-Host ("    SHA256SUMS.txt：{0:n0} 个文件，{1:n1} MB" -f $sums.Count, ((Get-Item -LiteralPath $sums.Path).Length / 1MB))
        }

        Step "收尾校验（同 -Verify）" {
            Invoke-Verify $release $version
        }
    } else {
        Write-Host ""
        Write-Host "注意：指定了 -SkipPayload，本次未复制 engine/models/runtime，未重写 使用说明.txt，也未生成 SHA256SUMS.txt。" -ForegroundColor DarkYellow
        Write-Host "     如需完整性校验：去掉 -SkipPayload 重新构建，或对已有包运行 -Verify。" -ForegroundColor DarkYellow
    }

    Step "汇总" {
        $files = @(Get-PackageFileList $release)
        $bytes = ($files | Measure-Object -Property Length -Sum).Sum
        $script:total.Stop()
        Write-Host ("    文件数 {0:n0}，总大小 {1:n2} GB" -f $files.Count, ($bytes / 1GB))
        Write-Host ("    总耗时 {0:n1}s" -f $script:total.Elapsed.TotalSeconds) -ForegroundColor Green
        Write-Host ("    产物：{0}" -f $release) -ForegroundColor Green
        Write-Host "    验收/性能测试会在包内产生 data\，已在重构建时备份到 _archive 并清除，绝不进交付包。" -ForegroundColor DarkGray
        if ($script:dataBackupDir) {
            Write-Host ("    本次 data 备份：{0}" -f $script:dataBackupDir) -ForegroundColor DarkGray
        }
    }

    Write-Host ""
    if ($SkipPayload) {
        Write-Host ("局部重建完成（-SkipPayload，非完整可交付包）：{0}（版本 {1}）" -f $release, $version) -ForegroundColor DarkYellow
    } else {
        Write-Host ("打包完成：{0}（版本 {1}）" -f $release, $version) -ForegroundColor Green
    }
    exit 0
} catch {
    Write-Host ""
    Write-Host ("打包失败：{0}" -f $_.Exception.Message) -ForegroundColor Red
    Write-Host "已复制的文件会保留，修复后可直接重跑（robocopy /MIR 会续传/同步相同文件）。" -ForegroundColor DarkYellow
    exit 1
}
