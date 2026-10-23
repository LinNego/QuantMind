# ============================================================
#  QuantMind 通达信桥 · 首次安装 / 环境自检
#
#  用法（bridge-windows 目录下，建议管理员 PowerShell）:
#     .\scripts\install.ps1                      # 交互式首次安装（独立运行）
#     .\scripts\install.ps1 -Token <64hex>       # 对接平台：写入与 Linux .env 相同的 token
#     .\scripts\install.ps1 -WithThs -WithAidata # 同时装同花顺旁路 / TdxAiData 付费通道
#     .\scripts\install.ps1 -Check               # 只做环境体检，不改任何东西
#     .\scripts\install.ps1 -DryRun              # 只打印计划
#
#  安装内容：
#     1. 校验 Python >= 3.10（拒绝 WindowsApps 别名），创建 .venv
#     2. 安装桥依赖（requirements.txt）
#     3. 生成/写入访问 token（config.yaml auth.token，ACL 收紧）
#     4. 可选：克隆同花顺 worker（vendor\ths + vendor\ths_deps）
#     5. 可选：安装 TdxAiData 付费通道（vendor\tdxaidata）
#     6. 防火墙放行 8550（局域网访问）
#     7. 注册三个计划任务（看门狗 / 采集器 / 同花顺 worker）
#     8. 启动并验收
#
#  说明：
#   - 本脚本幂等：重复运行不会轮换已有 token、不会覆盖配置其它内容；
#   - 计划任务与防火墙需要管理员权限，非管理员会自动跳过并给出提权命令；
#   - 旧版 setup.ps1 仍可用，但首次安装请用本脚本。
# ============================================================
param(
    [ValidateSet("standalone", "platform")]
    [string]$Deploy = "standalone",
    [string]$Token = "",
    [string]$PythonPath = "",
    [string]$IndexUrl = "",
    [switch]$WithThs,
    [switch]$WithAidata,
    [switch]$NoThs,
    [switch]$NoAidata,
    [switch]$NoVenv,
    [switch]$NoFirewall,
    [switch]$NoTasks,
    [switch]$NoStart,
    [switch]$Check,
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$configPath = Join-Path $root "config.yaml"
$venvDir = Join-Path $root ".venv"
$venvPy = Join-Path $venvDir "Scripts\python.exe"

$script:problems = @()
$script:warnings = @()

function Step([string]$text) { Write-Host "`n== $text ==" -ForegroundColor Cyan }
function Ok([string]$text) { Write-Host "  [OK]   $text" -ForegroundColor Green }
function Warn([string]$text) {
    Write-Host "  [WARN] $text" -ForegroundColor Yellow
    $script:warnings += $text
}
function Bad([string]$text) {
    Write-Host "  [FAIL] $text" -ForegroundColor Red
    $script:problems += $text
}
function Info([string]$text) { Write-Host "  $text" -ForegroundColor DarkGray }

function Test-Admin {
    try {
        $id = [Security.Principal.WindowsIdentity]::GetCurrent()
        $p = New-Object Security.Principal.WindowsPrincipal($id)
        return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } catch { return $false }
}

function Resolve-Python {
    if ($PythonPath) {
        if (Test-Path $PythonPath) { return (Resolve-Path $PythonPath).Path }
        Bad "指定的 PythonPath 不存在: $PythonPath"
        return $null
    }
    if (Test-Path $venvPy) { return $venvPy }
    foreach ($cand in @(
            "C:\Program Files\Python312\python.exe",
            "C:\Program Files\Python311\python.exe",
            "C:\Program Files\Python310\python.exe")) {
        if (Test-Path $cand) { return $cand }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { return $cmd.Source }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return "py" }
    return $null
}

function Get-PyVersion([string]$py) {
    try {
        $out = & $py -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        return [string]$out
    } catch { return "" }
}

function Invoke-Pip([string]$py, [string[]]$pipArgs, [string]$label) {
    if ($DryRun) { Info "DRY: $py -m pip $($pipArgs -join ' ')"; return $true }
    & $py -m pip @pipArgs
    if ($LASTEXITCODE -ne 0) {
        Bad "$label 失败（python -m pip $($pipArgs -join ' ')）"
        return $false
    }
    return $true
}

function Install-PyDeps([string]$py, [string[]]$pipArgs, [string]$label, [string]$importProbe) {
    if ($DryRun) { Info "DRY: $py -m pip $($pipArgs -join ' ')"; return $true }
    & $py -m pip @pipArgs
    if ($LASTEXITCODE -ne 0) {
        $idx = if ($IndexUrl) { $IndexUrl } else { "https://pypi.org/simple" }
        Warn "$label 首次失败（常见：pip 镜像 403），改用 $idx 重试 ..."
        # --index-url 必须放在子命令（install）之后，放最前面 pip 不认
        $retry = @($pipArgs[0], "--index-url", $idx) + $pipArgs[1..($pipArgs.Count - 1)]
        & $py -m pip @retry
        if ($LASTEXITCODE -ne 0) {
            if ($importProbe) {
                & $py -c $importProbe 2>$null
                if ($LASTEXITCODE -eq 0) {
                    Warn "$label 安装失败，但依赖已在当前解释器可用（沿用现有环境）"
                    return $true
                }
            }
            Bad "$label 失败；可指定镜像: -IndexUrl https://pypi.org/simple"
            return $false
        }
    }
    return $true
}

# ---------- 配置 token（块感知，不动其它内容） ----------
function Set-AuthToken([string]$value) {
    $raw = [IO.File]::ReadAllText($configPath)
    $lines = $raw -split "`r?`n"
    $inAuth = $false
    $done = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '^auth:\s*$') { $inAuth = $true; continue }
        if ($inAuth -and $lines[$i] -match '^\S') { break }  # 下一个顶级键
        if ($inAuth -and $lines[$i] -match '^(\s+token:\s*).*$') {
            $lines[$i] = $Matches[1] + '"' + $value + '"'
            $done = $true
            break
        }
    }
    if (-not $done) {
        Bad "config.yaml 里找不到 auth.token 行"
        return $false
    }
    $text = $lines -join "`r`n"
    if ($DryRun) {
        Info "DRY: 写入 auth.token（len=$($value.Length)）"
        return $true
    }
    [IO.File]::WriteAllText($configPath, $text, (New-Object Text.UTF8Encoding($false)))
    return $true
}

function Get-RawToken {
    $raw = [IO.File]::ReadAllText($configPath)
    $lines = $raw -split "`r?`n"
    $inAuth = $false
    foreach ($line in $lines) {
        if ($line -match '^auth:\s*$') { $inAuth = $true; continue }
        if ($inAuth -and $line -match '^\S') { break }
        if ($inAuth -and $line -match '^\s+token:\s*"?([^"]*)"?\s*$') {
            return $Matches[1].Trim()
        }
    }
    return ""
}

function New-Token {
    # 两个 GUID 去掉连字符 = 64 hex（约 244 bit），与平台 64 位 hex 口径一致
    return ((New-Guid).Guid.Replace('-', '') + (New-Guid).Guid.Replace('-', ''))
}

function Protect-ConfigAcl {
    if ($DryRun) { Info "DRY: icacls config.yaml 收紧 ACL"; return }
    $user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls $configPath /inheritance:r /grant:r "${user}:(R,W)" "*S-1-5-18:(R,W)" "*S-1-5-32-544:(R,W)" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "config.yaml ACL 已收紧（仅当前用户/SYSTEM/Admins）" }
    else { Warn "config.yaml ACL 收紧失败（可用管理员重跑）" }
}

# ---------- 环境体检 ----------
function Invoke-Check {
    Step "环境体检"
    Info "目录: $root"
    Info "管理员: $(if (Test-Admin) { '是' } else { '否（计划任务/防火墙需要管理员）' })"

    $py = Resolve-Python
    if (-not $py) {
        Bad "找不到 Python（需 3.10+，安装时勾选 Add to PATH）"
    } else {
        $ver = Get-PyVersion $py
        Info "Python: $py ($ver)"
        if (-not $ver) { Bad "Python 不可执行" }
        elseif ([version]$ver -lt [version]"3.10") { Bad "Python $ver < 3.10（代码使用了 3.10+ 语法）" }
        else { Ok "Python 版本可用" }
        if (Test-Path $venvPy) { Ok ".venv 已创建" } else { Warn ".venv 未创建（首次安装会创建）" }
        if (-not $DryRun) {
            & $py -c "import aiohttp, yaml" 2>$null
            if ($LASTEXITCODE -eq 0) { Ok "桥依赖 aiohttp/pyyaml 可导入" }
            else { Bad "桥依赖缺失（重新运行 install.ps1）" }
        }
    }

    $tok = Get-RawToken
    if (-not $tok) { Bad "config.yaml 未配置 token" }
    elseif ($tok -match '^\$\{.*\}$') { Warn "token 仍是占位符 ${tok}（需要 BRIDGE_AUTH_TOKEN 环境变量，否则拒绝启动）" }
    else { Ok "token 已配置（len=$($tok.Length)）" }

    foreach ($pair in @(@("vendor\ths", "同花顺 worker 上游"), @("vendor\ths_deps", "THS 依赖"), @("vendor\tdxaidata", "TdxAiData 付费通道"))) {
        $p = Join-Path $root $pair[0]
        if (Test-Path $p) { Ok "$($pair[1]) 存在（$($pair[0])）" }
        else { Warn "$($pair[1]) 缺失（$($pair[0])）" }
    }

    foreach ($t in @("TDXBridgeWatchdog", "TdxCollector", "TdxThsWorker")) {
        $task = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
        if ($task) { Ok "计划任务 ${t}: $($task.State)" }
        else { Warn "计划任务 $t 未注册" }
    }

    $fw = Get-NetFirewallRule -DisplayName "TDXBridge-8550" -ErrorAction SilentlyContinue
    if (-not $fw) {
        $portRule = Get-NetFirewallPortFilter -ErrorAction SilentlyContinue |
            Where-Object { $_.LocalPort -eq "8550" } | Select-Object -First 1
        $fw = $portRule
    }
    if ($fw) { Ok "防火墙已放行 8550" } else { Warn "防火墙未放行 8550" }

    foreach ($p in @(17709, 8550, 8560, 8765)) {
        $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
        $name = switch ($p) { 17709 { "TQ" } 8550 { "桥" } 8560 { "采集器" } 8765 { "同花顺" } }
        if ($c) { Ok "$name $p 监听中" } else { Info "$name $p 未监听" }
    }

    try { $h = Invoke-RestMethod "http://127.0.0.1:8550/api/v1/health" -TimeoutSec 4
        Ok "桥健康: tdx_connected=$($h.tdx_connected)" } catch { Info "桥 8550 暂不可达" }

    Write-Host ""
    if ($script:problems.Count -gt 0) {
        Write-Host "体检结果: $($script:problems.Count) 项失败 / $($script:warnings.Count) 项提醒" -ForegroundColor Red
        exit 1
    }
    Write-Host "体检结果: 通过（$($script:warnings.Count) 项提醒）" -ForegroundColor Green
}

# ============================================================
#  main
# ============================================================
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  QuantMind · 通达信桥 首次安装/环境自检" -ForegroundColor Cyan
Write-Host "  目录: $root" -ForegroundColor DarkGray
Write-Host "  模式: $Deploy$(if ($Check) { '（仅体检）' } elseif ($DryRun) { '（演练）' })" -ForegroundColor DarkGray
Write-Host "============================================" -ForegroundColor Cyan

if (-not (Test-Path $configPath)) {
    Bad "找不到 config.yaml，请在 bridge-windows 目录下运行"
    exit 1
}
if ($Check) { Invoke-Check; exit 0 }

# ---------- 1. Python ----------
Step "1/8 Python 环境"
$admin = Test-Admin
if (-not $admin) { Warn "非管理员：计划任务与防火墙会被跳过" }

$py = Resolve-Python
if (-not $py) {
    Bad "找不到 Python。请安装 Python 3.12（勾选 Add Python to PATH）: https://www.python.org/downloads/"
    exit 1
}
$ver = Get-PyVersion $py
Info "解释器: $py ($ver)"
if (-not $NoVenv -and -not (Test-Path $venvPy)) {
    if ($DryRun) {
        Info "DRY: 创建 .venv"
    } else {
        Info "创建 .venv ..."
        if ($py -eq "py") { & py -3 -m venv $venvDir } else { & $py -m venv $venvDir }
        if (-not (Test-Path $venvPy)) {
            Bad "创建 .venv 失败；可用 -NoVenv 使用系统 Python"
            exit 1
        }
    }
}
if (Test-Path $venvPy) { $py = $venvPy; Ok "使用 .venv: $py" }
else { Warn "未使用 .venv（-NoVenv），依赖将装到系统 Python" }

$ver = Get-PyVersion $py
if ([version]$ver -lt [version]"3.10") {
    Bad "Python $ver < 3.10，请升级（代码使用 3.10+ 语法）"
    exit 1
}

# ---------- 2. 依赖 ----------
Step "2/8 安装桥依赖"
if (-not $DryRun) {
    & $py -m pip install --quiet --upgrade pip 2>$null
    if ($LASTEXITCODE -ne 0) { Warn "pip 自升级失败（不影响安装，继续）" }
}
Install-PyDeps $py @("install", "--quiet", "-r", (Join-Path $root "requirements.txt")) "桥依赖安装" "import aiohttp, yaml" | Out-Null

# ---------- 3. token ----------
Step "3/8 访问 token"
$current = Get-RawToken
$resolvedToken = ""
if ($Token) {
    $resolvedToken = $Token.Trim()
    Info "使用命令行 -Token（平台对接模式）"
} elseif ($env:BRIDGE_AUTH_TOKEN) {
    $resolvedToken = $env:BRIDGE_AUTH_TOKEN.Trim()
    Info "使用环境变量 BRIDGE_AUTH_TOKEN"
} elseif ($current -and $current -notmatch '^\$\{.*\}$') {
    Ok "已有 token，保持不变（不会轮换）"
} elseif ($Deploy -eq "platform") {
    Bad "平台对接模式需要 -Token（与 Linux .env 的 TDX_BRIDGE_TOKEN 一致）"
    exit 1
} else {
    $resolvedToken = New-Token
    Write-Host ""
    Write-Host "  已生成首次安装 token（请立即保存，网页登录/平台对接都要用）:" -ForegroundColor Yellow
    Write-Host "    $resolvedToken" -ForegroundColor White
    Write-Host ""
}

if ($resolvedToken) {
    if ($resolvedToken.Length -lt 16 -or $resolvedToken -match '\s') {
        Bad "token 不合法（16–256 个非空白字符）"
        exit 1
    }
    if (-not (Set-AuthToken $resolvedToken)) { exit 1 }
    Ok "token 已写入 config.yaml（len=$($resolvedToken.Length)）"
}
Protect-ConfigAcl

# ---------- 4. 同花顺旁路 ----------
Step "4/8 同花顺旁路（可选）"
$thsVendor = Join-Path $root "vendor\ths"
$thsDeps = Join-Path $root "vendor\ths_deps"
$doThs = [bool]$WithThs   # 默认不装，需显式 -WithThs
if ($NoThs) { $doThs = $false }
if ($doThs) {
    if (-not (Test-Path $thsVendor)) {
        $git = Get-Command git -ErrorAction SilentlyContinue
        if (-not $git) {
            Bad "安装同花顺旁路需要 git（https://git-scm.com/download/win）"
        } elseif ($DryRun) {
            Info "DRY: git clone --depth 1 https://github.com/volvoai/thspypc.git vendor\ths"
        } else {
            Info "克隆 thspypc 到 vendor\ths ..."
            & git clone --depth 1 https://github.com/volvoai/thspypc.git $thsVendor
            if ($LASTEXITCODE -ne 0) { Bad "git clone 失败（检查网络/GitHub 访问）" }
        }
    } else { Ok "vendor\ths 已存在" }

    if ((Test-Path $thsVendor) -or $DryRun) {
        $depsArgs = @("install", "--quiet", "--target", $thsDeps, "--upgrade",
            "--disable-pip-version-check",
            "a-trade-calendar>=1.0", "pycryptodome>=3.20.0", "qrcode>=8.2",
            "requests>=2.28", "fastapi>=0.110", "uvicorn>=0.29",
            "websockets>=12", "httpx>=0.27", "colorama")
        Install-PyDeps $py $depsArgs "同花顺 worker 依赖安装" "" | Out-Null
        if (-not $DryRun -and (Test-Path $thsVendor)) {
            & $py -c "import sys; sys.path.insert(0, r'$(Join-Path $root 'vendor\ths\src')'); sys.path.insert(0, r'$thsDeps'); import thspypc; print('thspypc ok')" 2>$null
            if ($LASTEXITCODE -eq 0) { Ok "thspypc 可导入" } else { Bad "thspypc 导入失败（看上方 pip 输出）" }
        }
        $envExample = Join-Path $root "data\ths.env.example"
        if (-not (Test-Path $envExample)) {
            if (-not $DryRun) {
                New-Item -ItemType Directory -Path (Join-Path $root "data") -Force | Out-Null
                @("# 同花顺账号（二选一：填这里，或用网页扫码登录）",
                  "THS_USERNAME=", "THS_PASSWORD=", "THS_IMEI=") |
                    Set-Content -Path $envExample -Encoding UTF8
            }
            Info "已生成 data\ths.env.example（扫码登录可不填账号）"
        }
    }
} else {
    Info "跳过（需要时运行: .\scripts\install.ps1 -WithThs）"
}

# ---------- 5. TdxAiData ----------
Step "5/8 TdxAiData 付费通道（可选）"
$aidataVendor = Join-Path $root "vendor\tdxaidata"
$doAidata = $WithAidata -and -not $NoAidata
if ($doAidata) {
    if (Test-Path $aidataVendor) { Ok "vendor\tdxaidata 已存在" }
    else {
        Invoke-Pip $py @("install", "--quiet", "--target", (Join-Path $root "vendor"),
                          "--upgrade", "tdxaidata") "tdxaidata 安装" | Out-Null
        if (-not $DryRun -and -not (Test-Path $aidataVendor)) {
            Warn "pip 未装出 vendor\tdxaidata：可能不在公共 PyPI；请按发布包说明放置"
        }
    }
    Info "付费 Key 在网页「数据服务」里填写（写入 vendor\tdxaidata\lib\TdxAiData.ini）"
} else {
    Info "跳过（需要时运行: .\scripts\install.ps1 -WithAidata）"
}

# ---------- 6. 防火墙 ----------
Step "6/8 防火墙（8550）"
if ($NoFirewall) { Info "跳过（-NoFirewall）" }
elseif (-not $admin) { Warn "需要管理员；提权后运行: .\scripts\install.ps1 -NoStart" }
elseif ($DryRun) { Info "DRY: netsh 放行 TCP 8550" }
else {
    & netsh advfirewall firewall delete rule name="TDXBridge-8550" 2>$null | Out-Null
    & netsh advfirewall firewall add rule name="TDXBridge-8550" dir=in action=allow `
        protocol=TCP localport=8550 description="通达信交易桥 HTTP 通道" 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { Ok "已放行 TCP 8550（局域网）" } else { Bad "防火墙规则添加失败" }
}

# ---------- 7. 计划任务 ----------
Step "7/8 计划任务"
if ($NoTasks) { Info "跳过（-NoTasks）" }
elseif (-not $admin) { Warn "需要管理员；提权后运行: .\scripts\install.ps1 -NoStart" }
elseif ($DryRun) { Info "DRY: 注册 TDXBridgeWatchdog / TdxCollector / TdxThsWorker" }
else {
    & (Join-Path $PSScriptRoot "fix_watchdog.ps1") -NoStart | Out-Null
    Ok "TDXBridgeWatchdog 已注册"
    & (Join-Path $PSScriptRoot "install_collector_task.ps1") -NoStart | Out-Null
    Ok "TdxCollector 已注册"
    if ($doThs -and (Test-Path $thsVendor)) {
        & (Join-Path $PSScriptRoot "install_ths_task.ps1") -NoStart | Out-Null
        Ok "TdxThsWorker 已注册"
    } else {
        Info "TdxThsWorker 未注册（未装同花顺旁路）"
    }
}

# ---------- 8. 启动 ----------
Step "8/8 启动与验收"
if ($NoStart) { Info "跳过（-NoStart）；手动启动: 双击 启动交易台.cmd" }
elseif ($DryRun) { Info "DRY: 运行 scripts\launcher.ps1 -Restart -NoPause" }
else {
    & (Join-Path $PSScriptRoot "launcher.ps1") -Restart -NoPause
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  安装完成" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  网页控制台: http://127.0.0.1:8550/ui"
Write-Host "  局域网访问: http://<本机IP>:8550/ui （防火墙已放行 8550）"
Write-Host "  健康检查:   curl.exe http://127.0.0.1:8550/api/v1/health"
Write-Host "  环境体检:   .\scripts\install.ps1 -Check"
Write-Host "  日志目录:   data\logs\（bridge.log / collector.log / watchdog.log）"
if ($Deploy -eq "platform") {
    Write-Host "  平台对接:   Linux .env 里 TDX_BRIDGE_URL=http://<本机IP>:8550、TDX_BRIDGE_TOKEN=本次 -Token" -ForegroundColor Yellow
} elseif ($resolvedToken) {
    Write-Host "  访问 token: 已在上面打印并写入 config.yaml（网页右上角填入）" -ForegroundColor Yellow
}
Write-Host ""
if (-not $admin -and -not $NoTasks) {
    Write-Host "  提醒：本次不是管理员，计划任务/防火墙已跳过。请以管理员重跑一次：" -ForegroundColor Yellow
    Write-Host "        .\scripts\install.ps1 -NoStart" -ForegroundColor Yellow
}

if ($script:problems.Count -gt 0) {
    Write-Host "  有 $($script:problems.Count) 项失败，请按上面 [FAIL] 处理后再运行 -Check。" -ForegroundColor Red
    exit 1
}
