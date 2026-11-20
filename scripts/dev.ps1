#requires -Version 5.1
<#
  声迹 VoiceTrace - 开发启动脚本
  1) 若 5173 没有前端 dev server，则启动 #source/frontend 的 Vite；
  2) 调用 pnpm tauri dev（Rust 侧 devUrl 固定 http://localhost:5173）。
  退出时只结束本脚本启动的 Vite 进程树，不影响已在运行的 dev server。

  引擎由壳自己拉起：壳用 CSPRNG 生成 token 并只经环境变量 VT_TOKEN 传给子进程，
  本脚本不传 --token（命令行参数会进进程列表）。单独的引擎调试命令见 README.md。
  退出码跟随 pnpm tauri dev（0 正常，非 0 原样透传）。
#>
[CmdletBinding()]
param(
    [int]$VitePort = 5173,
    [switch]$NoFrontend
)

$ErrorActionPreference = 'Stop'

$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$frontend = Join-Path $root '#source\frontend'
$app = Join-Path $root '#source\app'
# 工作区共享 Rust 工具链：有就优先，没有就用 PATH 里的 cargo（不再因为缺 _tools 直接 throw）。
$cargoBin = Join-Path $root '..\_tools\cargo\bin'
if ((Test-Path (Join-Path $cargoBin 'cargo.exe')) -and ($env:PATH -notlike "*$cargoBin*")) {
    $env:PATH = "$cargoBin;$env:PATH"
}

function Write-Step([string]$message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

function Test-TcpPort([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Wait-TcpPort([int]$port, [int]$timeoutSec) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-TcpPort $port) { return $true }
        Start-Sleep -Milliseconds 400
    }
    return $false
}

if (-not (Get-Command pnpm -ErrorAction SilentlyContinue)) {
    throw "找不到 pnpm，请确认 Node.js / pnpm 已加入 PATH。"
}

$viteProcess = $null
$startedVite = $false
$script:exitCode = 0

try {
    if (-not $NoFrontend) {
        if (Test-TcpPort $VitePort) {
            Write-Host "复用已运行的 http://localhost:$VitePort" -ForegroundColor DarkGray
        } else {
            if (-not (Test-Path (Join-Path $frontend 'package.json'))) {
                throw "前端目录不完整：$frontend"
            }
            if (-not (Test-Path (Join-Path $frontend 'node_modules'))) {
                Write-Step "安装前端依赖"
                Push-Location $frontend
                try {
                    pnpm install --frozen-lockfile
                    if ($LASTEXITCODE -ne 0) { throw "pnpm install --frozen-lockfile 失败（exit $LASTEXITCODE），不再启动 Vite。" }
                } finally { Pop-Location }
            }
            Write-Step "启动前端 dev server（:$VitePort）"
            $viteProcess = Start-Process -FilePath 'pnpm.cmd' `
                -ArgumentList @('dev', '--port', "$VitePort") `
                -WorkingDirectory $frontend -PassThru -WindowStyle Hidden
            $startedVite = $true
            if (-not (Wait-TcpPort $VitePort 90)) {
                throw "前端 dev server 90 秒内没有监听 $VitePort，请在前端目录手动运行 pnpm dev 查看报错。"
            }
            Write-Host "前端已就绪：http://localhost:$VitePort" -ForegroundColor Green
        }
    }

    Write-Step "启动 Tauri（cargo 首次编译可能较久）"
    Push-Location $app
    try {
        pnpm tauri dev
        if ($LASTEXITCODE) { $script:exitCode = $LASTEXITCODE }
    } finally {
        Pop-Location
    }
} finally {
    if ($startedVite -and $viteProcess) {
        Write-Host ""
        Write-Host "==> 结束前端 dev server" -ForegroundColor DarkGray
        & taskkill.exe /PID $viteProcess.Id /T /F 2>$null | Out-Null
    }
}

exit $script:exitCode
