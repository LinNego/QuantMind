# ============================================================
#  QuantMind 交易台一键启动器
#
#  双击 启动交易台.cmd，或：
#      .\scripts\launcher.ps1              # 缺谁起谁，已在跑不动
#      .\scripts\launcher.ps1 -Restart     # 强制重启桥 + 采集器
#      .\scripts\launcher.ps1 -HotSet "600519.SH,000001.SZ"
#      .\scripts\launcher.ps1 -NoCollector -NoPause
#
#  顺序：TQ 17709（需 TdxW 已登录）→ 桥 8550 → 看门狗 → 采集器 8560
#  说明：TDX 客户端与 TQ 策略必须在通达信里手动登录/运行，本脚本只探测 17709。
# ============================================================
param(
    [switch]$Restart,
    [switch]$NoCollector,
    [switch]$NoWatchdog,
    [string]$HotSet = "",
    [switch]$NoPause
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv\Scripts\python.exe"
$py = if (Test-Path $venv) { $venv } elseif (Test-Path "C:\Program Files\Python312\python.exe") { "C:\Program Files\Python312\python.exe" } else { (Get-Command python).Source }
if (-not $py -or $py -like "*WindowsApps*") {
    Write-Host "[launcher] 找不到可用的 Python（拒绝 WindowsApps 别名）" -ForegroundColor Red
    if (-not $NoPause) { Read-Host "按回车关闭" | Out-Null }
    exit 1
}

function Test-Port([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}
function Get-Json([string]$Url) {
    try { return Invoke-RestMethod -Uri $Url -TimeoutSec 4 } catch { return $null }
}
function Stop-Port([int]$Port) {
    $c = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    foreach ($x in $c) {
        Stop-Process -Id $x.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "  QuantMind · 通达信交易台启动器" -ForegroundColor Cyan
Write-Host "  $root" -ForegroundColor DarkGray
Write-Host ""

# ---------- 1. TQ 17709 ----------
$tqOk = Test-Port 17709
if ($tqOk) { Write-Host "[1/4] TQ 17709       OK" -ForegroundColor Green }
else { Write-Host "[1/4] TQ 17709       未监听：请先启动 TdxW 并登录，再运行 TQ 常驻策略" -ForegroundColor Yellow }

# ---------- 2. 桥 8550 ----------
$bridge = if (-not $Restart) { Get-Json "http://127.0.0.1:8550/api/v1/health" } else { $null }
if ($Restart -or -not $bridge) {
    if ($Restart -or (Test-Port 8550)) {
        Write-Host "[2/4] 桥 8550        停止旧进程..." -ForegroundColor DarkGray
        Stop-Port 8550
        Start-Sleep -Seconds 2
    }
    Write-Host "[2/4] 桥 8550        启动中..." -ForegroundColor DarkGray
    Start-Process -FilePath $py `
        -ArgumentList "-m main --mode auto --config config.yaml" `
        -WorkingDirectory $root -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 8
    $bridge = Get-Json "http://127.0.0.1:8550/api/v1/health"
}
if ($bridge -and $bridge.status -eq "ok") {
    Write-Host ("[2/4] 桥 8550        OK  tdx_connected={0}" -f $bridge.tdx_connected) -ForegroundColor Green
} else {
    Write-Host "[2/4] 桥 8550        启动失败（看 data\logs\bridge.log）" -ForegroundColor Red
}

# ---------- 3. 看门狗 ----------
if (-not $NoWatchdog) {
    $wd = Get-ScheduledTask -TaskName "TDXBridgeWatchdog" -ErrorAction SilentlyContinue
    if ($wd) {
        if ($wd.State -ne "Running") { Start-ScheduledTask -TaskName "TDXBridgeWatchdog" }
        Write-Host "[3/4] 看门狗         OK（计划任务）" -ForegroundColor Green
    } else {
        Start-Process powershell -ArgumentList "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\watchdog.ps1`"" -WorkingDirectory $root -WindowStyle Hidden | Out-Null
        Write-Host "[3/4] 看门狗         已临时启动；建议跑 scripts\fix_watchdog.ps1 注册" -ForegroundColor Yellow
    }
} else {
    Write-Host "[3/4] 看门狗         跳过（-NoWatchdog）" -ForegroundColor DarkGray
}

# ---------- 4. 采集器 8560 ----------
if (-not $NoCollector) {
    # -HotSet 是显式意图：即使采集器在跑也要按新热集重启（只重启采集器，不动桥）
    $colRestart = $Restart -or [bool]$HotSet
    $col = if (-not $colRestart) { Get-Json "http://127.0.0.1:8560/health" } else { $null }
    if ($colRestart -or -not $col) {
        if ($colRestart -or (Test-Port 8560)) {
            Write-Host "[4/4] 采集器 8560    停止旧进程..." -ForegroundColor DarkGray
            Stop-Port 8560
            Start-Sleep -Seconds 1
        }
        $colTask = Get-ScheduledTask -TaskName "TdxCollector" -ErrorAction SilentlyContinue
        if ($colTask -and -not $HotSet) {
            Start-ScheduledTask -TaskName "TdxCollector"
        } else {
            $colArgLine = "scripts\tdx_collector.py --config config.yaml"
            if ($HotSet) { $colArgLine += " --hot-set `"$HotSet`"" }
            Start-Process -FilePath $py -ArgumentList $colArgLine -WorkingDirectory $root -WindowStyle Hidden | Out-Null
        }
        Start-Sleep -Seconds 5
        $col = Get-Json "http://127.0.0.1:8560/health"
    }
    if ($col -and $col.success -and $col.data.status -eq "ok") {
        Write-Host ("[4/4] 采集器 8560    OK  universe={0}" -f $col.data.universe_size) -ForegroundColor Green
    } else {
        Write-Host "[4/4] 采集器 8560    启动失败（看 data\logs\collector.log）" -ForegroundColor Red
    }
} else {
    Write-Host "[4/4] 采集器         跳过（-NoCollector）" -ForegroundColor DarkGray
}

# ---------- 汇总 ----------
Write-Host ""
Write-Host "  ------------------------- 状态 -------------------------" -ForegroundColor DarkGray
$h = Get-Json "http://127.0.0.1:8550/api/v1/health"
$c = Get-Json "http://127.0.0.1:8560/health"
$cdata = if ($c) { $c.data } else { $null }
$lim = if ($h) { $h.trade_limits.orders } else { $null }
$tqText = if ($tqOk) { "监听" } else { "未监听" }
$bridgeText = if ($h) { "ok / tdx=" + $h.tdx_connected } else { "不可达" }
$colText = if ($cdata) { $cdata.status + " / 宇宙 " + $cdata.universe_size } else { "不可达" }
$limText = if ($lim) { "{0}/{1}（本秒 {2}/{3}）" -f $lim.day_count, $lim.day_limit, $lim.last_sec_count, $lim.sec_limit } else { "--" }
Write-Host ("  TQ 17709    {0}" -f $tqText)
Write-Host ("  桥 8550     {0}" -f $bridgeText)
Write-Host ("  采集器 8560 {0}" -f $colText)
Write-Host ("  硬上限      {0}" -f $limText)
Write-Host "  --------------------------------------------------------" -ForegroundColor DarkGray
Write-Host ""

if (-not $NoPause) { Read-Host "按回车关闭" | Out-Null }
