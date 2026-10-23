# ============================================================
#  注册行情采集器开机任务（管理员）
#
#  用法（bridge-windows 目录下，管理员 PowerShell）:
#      .\scripts\install_collector_task.ps1            # 注册并启动
#      .\scripts\install_collector_task.ps1 -NoStart   # 只注册
#
#  说明：采集器（8560）与桥（8550）是独立进程。桥有 TDXBridgeWatchdog，
#  采集器此前只靠手动启动，重启机器就没了；本脚本补上开机自启 + 崩溃重启。
#  与一键启动器兼容：launcher.ps1 发现 TdxCollector 任务存在时走任务启动。
# ============================================================
param(
    [switch]$NoStart
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$taskName = "TdxCollector"

$venvPy = Join-Path $root ".venv\Scripts\python.exe"
$py = if (Test-Path $venvPy) { $venvPy } elseif (Test-Path "C:\Program Files\Python312\python.exe") {
    "C:\Program Files\Python312\python.exe"
} else { (Get-Command python).Source }
if (-not $py -or $py -like "*WindowsApps*") {
    Write-Host "[collector] 找不到可用的 Python（拒绝 WindowsApps 别名）" -ForegroundColor Red
    exit 1
}

$scriptPath = Join-Path $root "scripts\tdx_collector.py"
if (-not (Test-Path $scriptPath)) {
    Write-Host "[collector] 找不到 $scriptPath" -ForegroundColor Red
    exit 1
}

Write-Host "[collector] 停止旧任务/旧进程..."
Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*tdx_collector.py*" } |
    ForEach-Object {
        Write-Host "[collector]   杀 PID=$($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Start-Sleep -Seconds 2

Write-Host "[collector] 注册计划任务（AtLogOn + 崩溃重启）..."
$action = New-ScheduledTaskAction -Execute $py `
    -Argument "-u `"$scriptPath`" --config `"$root\config.yaml`"" `
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
Write-Host "[collector] 任务已注册"

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 8
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:8560/health" -TimeoutSec 6
        if ($h.success -and $h.data.status -eq "ok") {
            Write-Host ("[collector] 运行中 universe={0} pricevol={1}" -f `
                $h.data.universe_size, $h.data.last_pricevol_count) -ForegroundColor Green
        } else {
            Write-Warning "[collector] 健康异常: $($h | ConvertTo-Json -Depth 3)"
        }
    } catch {
        Write-Warning "[collector] 8560 不可达: $($_.Exception.Message)"
    }
}
