@echo off
setlocal EnableExtensions
rem VoiceTrace perf probe: two cold-start measurements, printed as a table.
rem The real implementation lives in the PowerShell block below the marker line
rem (cmd.exe never reads it because of "exit /b").
rem   run-perf.cmd                 engine cold start + portable cold start
rem   run-perf.cmd -EngineOnly     engine only
rem   run-perf.cmd -PortableOnly   portable only
rem   run-perf.cmd -Runs 3         repeat each item, report every run
rem   run-perf.cmd -FfmpegDir X    override ffmpeg directory
set "VT_ROOT=%~dp0..\"
set "VT_SELF=%~f0"
set "VT_PERF_ARGS=%*"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText($env:VT_SELF);$i=$s.LastIndexOf('#---PS---');if($i -lt 0){Write-Host 'run-perf: embedded PowerShell block not found';exit 2};Invoke-Expression $s.Substring($i+9)"
exit /b %errorlevel%
#---PS---
# VoiceTrace 性能测量（不打包、不改动源码；每次测量用 _scratch 下的独立数据目录，跑完删除。
# 便携 exe 通过 VOICETRACE_DATA_DIR 指向 _scratch\<日期>\perf-data\<guid>\data，
# 绝不触碰交付目录的 data\ 与 %LOCALAPPDATA%\VoiceTrace，也不删任何运行中实例的 port.json。）
$ErrorActionPreference = 'Stop'

$root      = $env:VT_ROOT.TrimEnd('\')
$argsText  = [string]$env:VT_PERF_ARGS
$release   = Join-Path $root '#release\声迹_portable'
$exePath   = Join-Path $release '声迹.exe'
$engineDir = Join-Path $root '#source\engine'
$venvPy    = Join-Path $engineDir '.venv\Scripts\python.exe'

if ($argsText -match '(-h|--help|/\?)') {
    Write-Host '用法：run-perf.cmd [-EngineOnly|-PortableOnly] [-Runs N] [-FfmpegDir <目录>]'
    Write-Host '  测量「引擎冷启动到 /api/health 200 的秒数」与「便携包冷启动（exe 启动到引擎端口就绪）」。'
    Write-Host '  不含 GPU 转录基准（那属于人工验收）。'
    exit 0
}

$engineOnly   = ($argsText -match '-EngineOnly')
$portableOnly = ($argsText -match '-PortableOnly')
if ($engineOnly -and $portableOnly) {
    Write-Host '[FAIL] -EngineOnly 与 -PortableOnly 互斥：同给会两项全跳过却报成功。只选一个，或都不给（两项都测）。' -ForegroundColor Red
    exit 2
}
$runs = 1
if ($argsText -match '-Runs\s+(\d+)') { $runs = [int]$Matches[1] }
if ($runs -lt 1) { $runs = 1 }
if ($runs -gt 10) { $runs = 10 }

$ffmpegDir = ''
if ($argsText -match '-FfmpegDir\s+"([^"]+)"') {
    $ffmpegDir = $Matches[1]
} elseif ($argsText -match '-FfmpegDir\s+([^\s]+)') {
    $ffmpegDir = $Matches[1]
}
if (-not $ffmpegDir) {
    if ($env:VT_FFMPEG) { $ffmpegDir = $env:VT_FFMPEG }
    elseif (Get-Command ffmpeg.exe -ErrorAction SilentlyContinue) { $ffmpegDir = Split-Path -Parent (Get-Command ffmpeg.exe).Source }
    else { $ffmpegDir = 'D:\ffmpeg\bin' }
}

function Get-DisplayWidth([string]$s) {
    $w = 0
    foreach ($ch in $s.ToCharArray()) {
        $c = [int][char]$ch
        if (($c -ge 0x1100 -and $c -le 0x115F) -or ($c -ge 0x2E80 -and $c -le 0xA4CF) -or
            ($c -ge 0xAC00 -and $c -le 0xD7A3) -or ($c -ge 0xF900 -and $c -le 0xFAFF) -or
            ($c -ge 0xFE30 -and $c -le 0xFE6F) -or ($c -ge 0xFF00 -and $c -le 0xFF60) -or
            ($c -ge 0xFFE0 -and $c -le 0xFFE6)) { $w += 2 } else { $w += 1 }
    }
    return $w
}

function Format-Row([string]$left, [string]$value, [string]$note, [int]$leftWidth) {
    $pad = $leftWidth - (Get-DisplayWidth $left)
    if ($pad -lt 1) { $pad = 1 }
    return ($left + (' ' * $pad) + ('{0,9}' -f $value) + '  ' + $note)
}

function Get-FreeTcpPort {
    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([System.Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()
    return $port
}

function New-HexToken {
    $bytes = New-Object byte[] 16
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Test-TcpPort([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(300)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Test-Health200([string]$url, [string]$token) {
    try {
        $headers = @{}
        if ($token) { $headers['Authorization'] = 'Bearer ' + $token }
        $resp = Invoke-WebRequest -Uri $url -Headers $headers -TimeoutSec 2 -UseBasicParsing
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Remove-Tree([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    if (-not (Get-Command node.exe -ErrorAction SilentlyContinue)) {
        throw "找不到 node.exe，无法递归删除 $path（工作区约定：递归删除只能走 node fs.rmSync）"
    }
    $env:VT_RM_PATH = $path
    try {
        & node.exe -e "const fs=require('fs');fs.rmSync(process.env.VT_RM_PATH,{recursive:true,force:true,maxRetries:3,retryDelay:200});"
        if ($LASTEXITCODE -ne 0) { throw "递归删除失败（node exit $LASTEXITCODE）：$path" }
    } finally {
        Remove-Item Env:\VT_RM_PATH -ErrorAction SilentlyContinue
    }
    if (Test-Path -LiteralPath $path) { throw "递归删除后目录仍存在：$path" }
}

function Stop-ProcessTree([int]$processId) {
    try { & taskkill.exe /PID $processId /T /F 2>$null | Out-Null } catch { }
}

$results = New-Object System.Collections.ArrayList
$failed  = $false

Write-Host ''
Write-Host '================ VoiceTrace 性能测量 ================' -ForegroundColor Yellow
Write-Host ("仓库：      {0}" -f $root)
Write-Host ("ffmpeg：    {0}" -f $ffmpegDir)
Write-Host ("重复次数：  {0}" -f $runs)
Write-Host ''

# ---------------------------------------------------------------------------
# 1) 引擎冷启动 -> /api/health 200
# ---------------------------------------------------------------------------
if (-not $portableOnly) {
    if (-not (Test-Path -LiteralPath $venvPy)) {
        Write-Host ("[FAIL] 找不到引擎 venv python：{0}" -f $venvPy) -ForegroundColor Red
        $failed = $true
    } elseif (-not (Test-Path -LiteralPath (Join-Path $engineDir 'models'))) {
        Write-Host ("[FAIL] 找不到模型目录：{0}" -f (Join-Path $engineDir 'models')) -ForegroundColor Red
        $failed = $true
    } else {
        Write-Host '[1/2] 引擎冷启动（venv Python -> /api/health 200）' -ForegroundColor Cyan
        for ($n = 1; $n -le $runs; $n++) {
            $port    = Get-FreeTcpPort
            $token   = New-HexToken
            $tempDir = Join-Path (Join-Path $root '_scratch\perf') ([guid]::NewGuid().ToString('N'))
            New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
            $env:VT_TOKEN = $token
            $engineArgs = @(
                '-m', 'voicetrace_engine',
                '--port', "$port",
                '--data-dir', (Join-Path $tempDir 'data'),
                '--models-dir', (Join-Path $engineDir 'models'),
                '--ffmpeg', $ffmpegDir,
                '--engine-root', $engineDir,
                '--port-file', (Join-Path $tempDir 'port.json')
            )
            $proc = $null
            $seconds = $null
            $note = ''
            try {
                $startArgs = @{
                    FilePath               = $venvPy
                    ArgumentList           = $engineArgs
                    WorkingDirectory       = $engineDir
                    PassThru               = $true
                    WindowStyle            = 'Hidden'
                    RedirectStandardOutput = (Join-Path $tempDir 'engine.out.log')
                    RedirectStandardError  = (Join-Path $tempDir 'engine.err.log')
                }
                $sw = [System.Diagnostics.Stopwatch]::StartNew()
                $proc = Start-Process @startArgs
                $deadline = (Get-Date).AddSeconds(180)
                $url = 'http://127.0.0.1:' + $port + '/api/health'
                while ((Get-Date) -lt $deadline) {
                    if ($proc.HasExited) { throw ("引擎提前退出（exit {0}），见 {1}" -f $proc.ExitCode, (Join-Path $tempDir 'engine.err.log')) }
                    if (Test-Health200 $url $token) { break }
                    Start-Sleep -Milliseconds 100
                }
                if (-not (Test-Health200 $url $token)) { throw '180 秒内 /api/health 没有返回 200' }
                $sw.Stop()
                $seconds = $sw.Elapsed.TotalSeconds
                $note = ("port {0}；torch 导入计入" -f $port)
            } catch {
                $failed = $true
                $note = $_.Exception.Message
            } finally {
                Remove-Item Env:\VT_TOKEN -ErrorAction SilentlyContinue
                if ($proc -and -not $proc.HasExited) { Stop-ProcessTree $proc.Id; Start-Sleep -Milliseconds 600 }
                Remove-Tree $tempDir
            }
            if ($seconds -ne $null) {
                [void]$results.Add(@{ Item = ('引擎冷启动 -> /health 200 #{0}' -f $n); Seconds = $seconds; Note = $note; Ok = $true; Skipped = $false })
            } else {
                [void]$results.Add(@{ Item = ('引擎冷启动 -> /health 200 #{0}' -f $n); Seconds = $null; Note = $note; Ok = $false; Skipped = $false })
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 2) 便携包冷启动（exe 启动 -> port.json 出现 -> 引擎端口可连）
# ---------------------------------------------------------------------------
if (-not $engineOnly) {
    Write-Host ''
    if (-not (Test-Path -LiteralPath $exePath)) {
        Write-Host ("[SKIP] 未产出便携包：{0} 不存在（先跑 build-release.cmd）" -f $exePath) -ForegroundColor DarkYellow
        [void]$results.Add(@{ Item = '便携包冷启动'; Seconds = $null; Note = '未产出便携包（跳过）'; Ok = $true; Skipped = $true })
    } else {
        Write-Host '[2/2] 便携包冷启动（会短暂弹出程序窗口）' -ForegroundColor Cyan
        for ($n = 1; $n -le $runs; $n++) {
            $exeProc = $null
            $seconds = $null
            $note = ''
            # 隔离数据目录：VOICETRACE_DATA_DIR 优先于 exe 同级 data\（engine.rs default_data_dir），
            # 不污染交付目录，也不动 %LOCALAPPDATA%\VoiceTrace（可能有运行中实例的 port.json）。
            $tempDir = Join-Path (Join-Path $root ('_scratch\{0}\perf-data' -f (Get-Date -Format 'yyyy-MM-dd'))) ([guid]::NewGuid().ToString('N'))
            $dataDir = Join-Path $tempDir 'data'
            $portFile = Join-Path $dataDir 'port.json'
            New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
            try {
                $env:VOICETRACE_DATA_DIR = $dataDir
                $sw = [System.Diagnostics.Stopwatch]::StartNew()
                $exeProc = Start-Process -FilePath $exePath -WorkingDirectory $release -PassThru
                $deadline = (Get-Date).AddSeconds(240)
                $port = 0
                while ((Get-Date) -lt $deadline) {
                    if ($exeProc.HasExited) { throw ("声迹.exe 提前退出（exit {0}）" -f $exeProc.ExitCode) }
                    if (Test-Path -LiteralPath $portFile) {
                        try {
                            $json = Get-Content -LiteralPath $portFile -Raw | ConvertFrom-Json
                            if ($json.port) {
                                $port = [int]$json.port
                                if (Test-TcpPort $port) { break }
                            }
                        } catch { }
                    }
                    Start-Sleep -Milliseconds 100
                }
                if ($port -le 0 -or -not (Test-TcpPort $port)) { throw ("240 秒内引擎端口没有就绪（检查 {0}\logs\supervisor.log）" -f $dataDir) }
                $sw.Stop()
                $seconds = $sw.Elapsed.TotalSeconds
                $note = ("exe -> port.json -> TCP {0}；含 WebView2 启动；隔离数据目录" -f $port)
            } catch {
                $failed = $true
                $note = $_.Exception.Message
            } finally {
                Remove-Item Env:\VOICETRACE_DATA_DIR -ErrorAction SilentlyContinue
                if ($exeProc -and -not $exeProc.HasExited) {
                    try { $exeProc.CloseMainWindow() | Out-Null; $exeProc.WaitForExit(5000) | Out-Null } catch { }
                    if (-not $exeProc.HasExited) { Stop-ProcessTree $exeProc.Id }
                }
                Start-Sleep -Milliseconds 600
                # 只清理本次隔离目录（含其中的 port.json），绝不碰交付目录与 LOCALAPPDATA。
                Remove-Tree $tempDir
            }
            if ($seconds -ne $null) {
                [void]$results.Add(@{ Item = ('便携包冷启动 #{0}' -f $n); Seconds = $seconds; Note = $note; Ok = $true; Skipped = $false })
            } else {
                [void]$results.Add(@{ Item = ('便携包冷启动 #{0}' -f $n); Seconds = $null; Note = $note; Ok = $false; Skipped = $false })
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 表格
# ---------------------------------------------------------------------------
Write-Host ''
$leftWidth = 34
Write-Host (Format-Row '项目' '耗时' '说明' $leftWidth) -ForegroundColor Cyan
Write-Host ('-' * 80) -ForegroundColor DarkGray
foreach ($r in $results) {
    if ($r.Skipped) {
        Write-Host (Format-Row $r.Item '--' $r.Note $leftWidth) -ForegroundColor DarkYellow
    } elseif ($r.Seconds -ne $null) {
        Write-Host (Format-Row $r.Item ('{0:n2} s' -f $r.Seconds) $r.Note $leftWidth) -ForegroundColor Green
    } else {
        Write-Host (Format-Row $r.Item 'FAILED' $r.Note $leftWidth) -ForegroundColor Red
    }
}
Write-Host ''
Write-Host '说明：GPU 转录基准（RTF / 显存峰值）不属于本脚本，按 docs/VERIFY.md 人工验收。' -ForegroundColor DarkGray
Write-Host ''

if ($failed) { exit 1 }
exit 0
