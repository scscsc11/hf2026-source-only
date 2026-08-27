# verify.ps1 — Simulation release 包健康检查（Windows 版）
# 检查前端基础设施（start.ps1 起的，不含引擎——引擎要点赛题后才起）。

#Requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

$PACK_ROOT = $PSScriptRoot

# 读取 start.ps1 写入的实际端口
$envFile = Join-Path $PACK_ROOT 'run\env.ps1'
if (Test-Path $envFile) {
    . $envFile
}
$OPENSIM_REDIS_PORT = if ($env:OPENSIM_REDIS_PORT) { [int]$env:OPENSIM_REDIS_PORT } else { 6379 }
$OPENSIM_WS_PORT    = if ($env:OPENSIM_WS_PORT)    { [int]$env:OPENSIM_WS_PORT }    else { 8080 }
$OPENSIM_CAM_PORT   = if ($env:OPENSIM_CAM_PORT)   { [int]$env:OPENSIM_CAM_PORT }   else { 8081 }
$OPENSIM_WEB_PORT   = if ($env:OPENSIM_WEB_PORT)   { [int]$env:OPENSIM_WEB_PORT }   else { 3000 }

$PASS = 0
$FAIL = 0

function Invoke-Ok {
    param([string]$Msg)
    Write-Host "  ✓ $Msg" -ForegroundColor Green
    $script:PASS++
}
function Invoke-Fail {
    param([string]$Msg)
    Write-Host "  ✗ $Msg" -ForegroundColor Red
    $script:FAIL++
}

Write-Host '=== Simulation 健康检查 ==='

# 1. Redis
$redisCli = Join-Path $PACK_ROOT 'bin\redis-cli.exe'
if (Test-Path $redisCli) {
    $result = & $redisCli -p $OPENSIM_REDIS_PORT ping 2>$null
    if ($result -eq 'PONG') {
        Invoke-Ok "Redis (port $OPENSIM_REDIS_PORT) PONG"
    } else {
        Invoke-Fail "Redis (port $OPENSIM_REDIS_PORT) 无响应"
    }
} else {
    Invoke-Fail "redis-cli 缺失: $redisCli"
}

# 2. bridge HTTP
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$OPENSIM_CAM_PORT/api/sim/status" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
    if ($resp.StatusCode -eq 200) {
        Invoke-Ok "bridge HTTP (port $OPENSIM_CAM_PORT) /api/sim/status 200"
    } else {
        Invoke-Fail "bridge HTTP (port $OPENSIM_CAM_PORT) 状态码 $($resp.StatusCode)"
    }
} catch {
    Invoke-Fail "bridge HTTP (port $OPENSIM_CAM_PORT) 无响应 — 见 run\logs\bridge.log"
}

# 3. 前端静态服务
try {
    $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$OPENSIM_WEB_PORT/" -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
    if ($resp.Content -match '<html') {
        Invoke-Ok "前端静态服务 (port $OPENSIM_WEB_PORT) 返回 HTML"
    } else {
        Invoke-Fail "前端静态服务 (port $OPENSIM_WEB_PORT) 返回非 HTML"
    }
} catch {
    Invoke-Fail "前端静态服务 (port $OPENSIM_WEB_PORT) 无响应 — 见 run\logs\frontend.log"
}

# 4. Python 依赖
$python = $null
$candidates = @(
    'C:\Python314\python.exe',
    'C:\Python313\python.exe',
    'C:\Python312\python.exe',
    'C:\Python311\python.exe'
)
foreach ($c in $candidates) {
    if (Test-Path $c) { $python = $c; break }
}
if (-not $python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $python = 'python' }
}
if (-not $python) {
    $cmd = Get-Command py -ErrorAction SilentlyContinue
    if ($cmd) { $python = 'py' }
}

if ($python) {
    Invoke-Ok "python 可用: $python"
    & $python -c 'import redis, yaml' 2>$null
    if ($LASTEXITCODE -eq 0) {
        Invoke-Ok 'Python 包 redis + pyyaml 就绪'
    } else {
        Invoke-Fail 'Python 包 redis/pyyaml 缺失 — 运行 .\setup.ps1 或 pip install redis pyyaml'
    }
} else {
    Invoke-Fail 'python 不可用 — 运行 .\setup.ps1'
}

# 5. 引擎二进制存在性（Windows 用 .exe 后缀）
$simExe = Join-Path $PACK_ROOT 'opensim-sim.exe'
if (Test-Path -Path $simExe -PathType Leaf) {
    Invoke-Ok 'opensim-sim.exe 二进制就位'
} else {
    Invoke-Fail 'opensim-sim.exe 二进制缺失'
}

Write-Host ''
Write-Host "结果: $PASS 通过, $FAIL 失败"
if ($FAIL -eq 0) {
    Write-Host '✓ 全部健康' -ForegroundColor Green
} else {
    Write-Host '⚠️  有失败项，详见上方' -ForegroundColor Yellow
}
exit $FAIL
