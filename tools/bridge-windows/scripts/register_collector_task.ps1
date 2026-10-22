# ============================================================
#  注册/更新 TdxCollector 计划任务（独立行情采集器）
#
#  用法（bridge-windows 目录下，管理员 PowerShell）:
#      .\scripts\register_collector_task.ps1 -Start
#      .\scripts\register_collector_task.ps1 -Start -HotSet "600519.SH,000001.SZ"
# ============================================================
param(
    [switch]$Start,
    [string]$HotSet = "",
    [int]$Port = 8560
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
# 优先用桥同款解释器；避免 Windows Store 的 python 别名
$candidates = @(
    (Join-Path $root ".venv\Scripts\python.exe"),
    "C:\Program Files\Python312\python.exe"
)
$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) { $py = (Get-Command python).Source }
if (-not $py) { throw "找不到 python.exe" }

$arg = "`"$root\scripts\tdx_collector.py`" --config `"$root\config.yaml`" --port $Port"
if ($HotSet) { $arg += " --hot-set `"$HotSet`"" }

$taskName = "TdxCollector"
$action = New-ScheduledTaskAction -Execute $py -Argument $arg -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Write-Host "[collector] 计划任务已注册: $taskName"

# 防火墙放行采集器只读端口（仅私网）
try {
    if (-not (Get-NetFirewallRule -DisplayName "QuantMind TDX Collector $Port" -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName "QuantMind TDX Collector $Port" `
            -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow `
            -Profile Private | Out-Null
        Write-Host "[collector] 防火墙已放行 TCP $Port (Private)"
    }
} catch {
    Write-Warning "[collector] 防火墙规则失败: $($_.Exception.Message)"
}

if ($Start) {
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 6
    $info = Get-ScheduledTaskInfo -TaskName $taskName
    Write-Host "[collector] State=$((Get-ScheduledTask -TaskName $taskName).State) LastRun=$($info.LastRunTime)"
    try {
        $h = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 10
        Write-Host "[collector] health: status=$($h.data.status) bridge=$($h.data.bridge.status) universe=$($h.data.universe_size)"
    } catch {
        Write-Warning "[collector] health 失败: $($_.Exception.Message)"
    }
}
