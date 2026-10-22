# ============================================================
#  修复看门狗：停任务 → 杀旧 watchdog 进程 → 重注册 → 启动 → 验证
#
#  用法（bridge-windows 目录下，管理员 PowerShell）:
#      .\scripts\fix_watchdog.ps1
# ============================================================
param(
    [switch]$NoStart
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$taskName = "TDXBridgeWatchdog"

Write-Host "[watchdog] 停止旧任务..."
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

Write-Host "[watchdog] 杀旧 watchdog 进程..."
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
    Where-Object { $_.CommandLine -like "*watchdog.ps1*" } |
    ForEach-Object {
        Write-Host "[watchdog]   PID=$($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 1

Write-Host "[watchdog] 重新注册任务..."
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\watchdog.ps1`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Highest
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "[watchdog] 任务已重注册"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 8
    $p = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -like "*watchdog.ps1*" }
    if ($p) {
        Write-Host "[watchdog] 运行中 PID=$($p.ProcessId)"
    } else {
        Write-Warning "[watchdog] 未发现 watchdog 进程（看日志）"
    }
    $log = Join-Path $root "data\logs\watchdog.log"
    if (Test-Path $log) { Get-Content $log -Tail 5 }
}
