# ============================================================
#  同花顺 worker 一键升级（上游更新 / 回滚）
#
#  用法（bridge-windows 目录下，管理员 PowerShell）:
#      .\scripts\update_ths_worker.ps1              # 拉到最新并重装依赖 + 冒烟
#      .\scripts\update_ths_worker.ps1 -Ref <sha>   # 指定 commit/tag
#      .\scripts\update_ths_worker.ps1 -Rollback    # 回滚到上次成功版本
#      .\scripts\update_ths_worker.ps1 -NoRestart   # 只更新代码，不重启
#
#  设计：
#  - 上游代码在 vendor\ths（git clone，--depth 1）；
#  - 依赖装到 vendor\ths_deps（与桥/TQ 的全局 Python 隔离）；
#  - 每次升级前记录当前 commit 到 data\ths_upstream.txt，冒烟失败自动回滚；
#  - 冒烟 = /api/status 可达；已登录时再打一次 /api/quote。
# ============================================================
param(
    [string]$Ref = "",
    [switch]$Rollback,
    [switch]$NoRestart
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$ths = Join-Path $root "vendor\ths"
$deps = Join-Path $root "vendor\ths_deps"
$wheels = Join-Path $root "vendor\ths_wheels"
$py = "C:\Program Files\Python312\python.exe"
$versionFile = Join-Path $root "data\ths_upstream.txt"
$taskName = "TdxThsWorker"
$baseUrl = "http://127.0.0.1:8765"

function Get-Head { (git -C $ths rev-parse HEAD).Trim() }

function Restart-Worker {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Get-NetTCPConnection -LocalPort 8765 -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName $taskName
    Start-Sleep -Seconds 10
}

function Test-Smoke {
    try {
        $s = Invoke-RestMethod "$baseUrl/api/status" -TimeoutSec 10
        Write-Host ("[ths] smoke: status ok, connected={0} credentials={1}" -f `
            $s.connected, $s.credentials)
        if ($s.connected) {
            $q = Invoke-RestMethod "$baseUrl/api/quote?codes=600519.SH" -TimeoutSec 15
            Write-Host ("[ths] smoke: quote ok, rows={0}" -f @($q).Count)
        }
        return $true
    } catch {
        Write-Warning "[ths] smoke 失败: $($_.Exception.Message)"
        return $false
    }
}

function Install-Deps {
    # 先尝试在线（阿里云镜像）；失败退回本地 wheel（离线可复现）
    & $py -m pip install --target $deps --upgrade --disable-pip-version-check `
        -i https://mirrors.aliyun.com/pypi/simple/ `
        a-trade-calendar pycryptodome qrcode requests fastapi uvicorn websockets httpx colorama 2>&1 |
        Select-Object -Last 3
    if ($LASTEXITCODE -ne 0 -and (Test-Path $wheels)) {
        Write-Host "[ths] 在线安装失败，改用本地 wheel: $wheels"
        & $py -m pip install --no-index --find-links $wheels --target $deps --upgrade `
            --disable-pip-version-check `
            a-trade-calendar pycryptodome qrcode requests fastapi uvicorn websockets httpx colorama 2>&1 |
            Select-Object -Last 3
    }
}

if (-not (Test-Path (Join-Path $ths ".git"))) {
    Write-Host "[ths] vendor\ths 不是 git 仓库，请先 git clone" -ForegroundColor Red
    exit 1
}

if ($Rollback) {
    if (-not (Test-Path $versionFile)) { Write-Host "[ths] 没有记录可回滚" -ForegroundColor Red; exit 1 }
    $prev = (Get-Content $versionFile -Encoding UTF8 | Select-Object -First 1).Trim()
    if (-not $prev) { Write-Host "[ths] 回滚记录为空" -ForegroundColor Red; exit 1 }
    Write-Host "[ths] 回滚到 $prev"
    git -C $ths reset --hard $prev | Out-Null
} else {
    $old = Get-Head
    Set-Content -Path $versionFile -Value $old -Encoding UTF8
    Write-Host "[ths] 当前 $old，开始拉取 $(if ($Ref) { $Ref } else { 'origin/main' })"
    if ($Ref) {
        git -C $ths fetch --depth 1 origin $Ref 2>&1 | Select-Object -Last 2
    } else {
        git -C $ths fetch --depth 1 origin 2>&1 | Select-Object -Last 2
    }
    if ($LASTEXITCODE -ne 0) { Write-Host "[ths] fetch 失败，未做任何改动" -ForegroundColor Red; exit 1 }
    $target = if ($Ref) { "FETCH_HEAD" } else { "origin/HEAD" }
    git -C $ths reset --hard $target 2>&1 | Select-Object -Last 1
    Write-Host "[ths] 已更新到 $(Get-Head)"
}

Install-Deps

if ($NoRestart) { Write-Host "[ths] 跳过重启"; exit 0 }

Restart-Worker
if (Test-Smoke) {
    Write-Host "[ths] 升级成功：$(Get-Head)" -ForegroundColor Green
    exit 0
}

Write-Host "[ths] 升级后冒烟失败，回滚" -ForegroundColor Yellow
if (Test-Path $versionFile) {
    $prev = (Get-Content $versionFile -Encoding UTF8 | Select-Object -First 1).Trim()
    if ($prev) { git -C $ths reset --hard $prev | Out-Null }
}
Restart-Worker
if (Test-Smoke) { Write-Host "[ths] 已回滚并恢复" -ForegroundColor Yellow } else { Write-Host "[ths] 回滚后仍失败，请人工检查" -ForegroundColor Red }
exit 1
