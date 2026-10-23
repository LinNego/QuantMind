# ============================================================
#  注册同花顺 worker 开机任务（管理员）
#
#  用法（bridge-windows 目录下，管理员 PowerShell）:
#      .\scripts\install_ths_task.ps1            # 注册并启动
#      .\scripts\install_ths_task.ps1 -NoStart   # 只注册
#
#  说明：worker 只绑 127.0.0.1:8765，绝不对局域网开放；
#  凭据放 data\ths.env（THS_USERNAME / THS_PASSWORD / THS_IMEI）。
# ============================================================
param(
    [switch]$NoStart
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$taskName = "TdxThsWorker"
$py = "C:\Program Files\Python312\python.exe"
$scriptPath = Join-Path $root "scripts\ths_worker.py"

if (-not (Test-Path $scriptPath)) {
    Write-Host "[ths] 找不到 $scriptPath" -ForegroundColor Red
    exit 1
}

Write-Host "[ths] 停止旧任务/旧进程..."
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

Write-Host "[ths] 注册计划任务（AtLogOn + 崩溃重启）..."
$action = New-ScheduledTaskAction -Execute $py `
    -Argument "-u `"$scriptPath`" --host 127.0.0.1 --port 8765" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId ((whoami).Trim()) `
    -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "[ths] 任务已注册"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 10
    try {
        $s = Invoke-RestMethod "http://127.0.0.1:8765/api/status" -TimeoutSec 8
        Write-Host ("[ths] 运行中 connected={0} credentials={1}" -f `
            $s.connected, $s.credentials) -ForegroundColor Green
        if (-not $s.credentials) {
            Write-Host "      未配置账号：把 THS_USERNAME/THS_PASSWORD 写进 data\ths.env 后重启任务" -ForegroundColor Yellow
        }
    } catch {
        Write-Warning "[ths] 8765 不可达: $($_.Exception.Message)"
    }
}
