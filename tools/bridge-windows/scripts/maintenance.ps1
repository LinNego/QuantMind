# ============================================================
#  通达信桥维护：停桥 → 清理缓存日志 →（可选 VACUUM）→ 启桥 → 启用看门狗
#
#  用法（在 bridge-windows 目录下）:
#      .\scripts\maintenance.ps1                 # 30 天 / 300 万行 + VACUUM
#      .\scripts\maintenance.ps1 -NoVacuum       # 只删行，不缩文件
#      .\scripts\maintenance.ps1 -TdxDays 90 -TdxMaxRows 6000000
#
#  说明：
#  - VACUUM 需要独占锁，脚本会先停桥；结束后无论清理成败都会把桥拉起；
#  - 启动后等待 8s 做健康检查，再启动 TDXBridgeWatchdog 计划任务；
#  - 不删除 trade_log（交易台账），只清 tdx_log。
# ============================================================
param(
    [int]$TdxDays = 30,
    [int]$TdxMaxRows = 3000000,
    [switch]$NoVacuum
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv\Scripts\python.exe"
$py = if (Test-Path $venv) { $venv } else { "C:\Program Files\Python312\python.exe" }
if (-not (Test-Path $py)) { $py = (Get-Command python).Source }

Write-Host "[maint] 桥目录: $root"
Write-Host "[maint] Python: $py"

# 1. 停桥
$conns = Get-NetTCPConnection -LocalPort 8550 -State Listen -ErrorAction SilentlyContinue
if ($conns) {
    foreach ($c in $conns) {
        Write-Host "[maint] 停止桥进程 PID=$($c.OwningProcess)"
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
} else {
    Write-Host "[maint] 8550 无监听进程"
}

# 2. 删除旧日志（升级前那份 1.5GB，用户已确认）
$oldLog = Join-Path $root "data\logs\bridge.log.1"
if (Test-Path $oldLog) {
    $gb = [math]::Round((Get-Item $oldLog).Length / 1GB, 2)
    Remove-Item $oldLog -Force
    Write-Host "[maint] 已删除旧日志 bridge.log.1 (${gb} GB)"
}

# 3. 清理 + VACUUM
$pruneArgs = @("scripts\prune_cache.py", "--tdx-days", $TdxDays,
               "--tdx-max-rows", $TdxMaxRows)
if (-not $NoVacuum) { $pruneArgs += "--vacuum" }
Write-Host "[maint] 开始清理: $($pruneArgs -join ' ')"
try {
    & $py @pruneArgs
    Write-Host "[maint] 清理完成，退出码 $LASTEXITCODE"
} catch {
    Write-Warning "[maint] 清理异常: $($_.Exception.Message)"
}

# 4. 启桥（无论清理成败都要拉起）
Write-Host "[maint] 启动桥 (源码模式)..."
Start-Process -FilePath $py `
    -ArgumentList "-m main --mode auto --config config.yaml" `
    -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 8
try {
    $h = Invoke-RestMethod "http://127.0.0.1:8550/api/v1/health" -TimeoutSec 15
    Write-Host "[maint] 健康: $($h.status) / tdx_connected=$($h.tdx_connected)"
} catch {
    Write-Warning "[maint] 健康检查失败: $($_.Exception.Message)"
}

# 5. 清掉可能残留的重启 flag，再启用看门狗
$flag = Join-Path $root "restart_bridge.flag"
if (Test-Path $flag) {
    Remove-Item $flag -Force
    Write-Host "[maint] 已清除残留 restart_bridge.flag"
}
Write-Host "[maint] 启动 TDXBridgeWatchdog 计划任务..."
Start-ScheduledTask -TaskName "TDXBridgeWatchdog"
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName "TDXBridgeWatchdog"
$info = Get-ScheduledTaskInfo -TaskName "TDXBridgeWatchdog"
Write-Host "[maint] 看门狗 State=$($task.State) LastRun=$($info.LastRunTime)"

Write-Host "[maint] 完成。"
