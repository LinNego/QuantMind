# ============================================================
#  夜间一键切换：桥维护（停桥/清理/VACUUM/启桥/看门狗）+ 采集器
#
#  用法（bridge-windows 目录下，管理员 PowerShell）:
#      .\scripts\night_setup.ps1
#      .\scripts\night_setup.ps1 -HotSet "600519.SH,000001.SZ"
# ============================================================
param(
    [string]$HotSet = ""
)

$ErrorActionPreference = "Continue"
$here = $PSScriptRoot

Write-Host "=== 1/2 桥维护（停桥 → 清理 → VACUUM → 启桥 → 看门狗）==="
& (Join-Path $here "maintenance.ps1")

Write-Host "=== 2/2 注册并启动采集器 ==="
& (Join-Path $here "register_collector_task.ps1") -Start -HotSet $HotSet

Write-Host "=== 验证 ==="
try {
    $h = Invoke-RestMethod "http://127.0.0.1:8550/api/v1/health" -TimeoutSec 10
    Write-Host "bridge: status=$($h.status) tdx_connected=$($h.tdx_connected) slots=$($h.semaphores.trade.slots)/$($h.semaphores.data.slots)"
} catch {
    Write-Warning "bridge health 失败: $($_.Exception.Message)"
}
try {
    $c = Invoke-RestMethod "http://127.0.0.1:8560/health" -TimeoutSec 10
    Write-Host "collector: status=$($c.data.status) universe=$($c.data.universe_size) snapshot_age_ms=$($c.data.snapshot_age_ms)"
} catch {
    Write-Warning "collector health 失败: $($_.Exception.Message)"
}
Write-Host "完成。"
