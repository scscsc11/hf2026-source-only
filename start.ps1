# start.ps1 — Simulation release 包一键启动（Windows 版）
# 启动 Redis + bridge + 前端静态服务。引擎(sim)由 competition 按需 spawn;
# UE 渲染器在 UE 版包(检测到 config\renderers\ue_testwl.json 且非 template)时
# 由本脚本自动起一个前台窗口(service 模式),这样用户「点开始仿真」即有相机画面 ——
# 否则 UE 不启动 → bridge 收不到 renderer_online → 无可用相机流。

#Requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# 推导包根（脚本所在目录）
$PACK_ROOT = $PSScriptRoot
Set-Location $PACK_ROOT

# 端口可配（默认 + env 覆盖）
$OPENSIM_REDIS_PORT = if ($env:OPENSIM_REDIS_PORT) { [int]$env:OPENSIM_REDIS_PORT } else { 6379 }
$OPENSIM_WS_PORT    = if ($env:OPENSIM_WS_PORT)    { [int]$env:OPENSIM_WS_PORT }    else { 8080 }
$OPENSIM_CAM_PORT   = if ($env:OPENSIM_CAM_PORT)   { [int]$env:OPENSIM_CAM_PORT }   else { 8081 }
$OPENSIM_CAM_WS_PORT = if ($env:OPENSIM_CAM_WS_PORT) { [int]$env:OPENSIM_CAM_WS_PORT } else { 8082 }
$OPENSIM_WEB_PORT   = if ($env:OPENSIM_WEB_PORT)   { [int]$env:OPENSIM_WEB_PORT }   else { 3000 }

$RUN_DIR  = Join-Path $PACK_ROOT 'run'
$LOG_DIR  = Join-Path $RUN_DIR 'logs'
$PID_DIR  = Join-Path $RUN_DIR 'pids'

$dirs = @($LOG_DIR, $PID_DIR, (Join-Path $RUN_DIR 'redis'))
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# ── 工具函数 ──
function Test-PortInUse {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return $null -ne $conn -and @($conn).Count -gt 0
    } catch {
        return $false
    }
}

function Pick-FreePort {
    # -Taken 传入本脚本此前已分配的端口集合。Pick-FreePort 只检测系统 listen 端口,
    # 看不到"本脚本即将启动但还没 listen"的端口;若不带上 -Taken,5 个端口会被
    # 分到同一空闲端口(如 8080/8081 被系统占用时,WS 与 CAM_WS 都会落在 8082,
    # bridge 启动后 CameraWs listen 报 EADDRINUSE,相机画面起不来)。
    param([int]$Start, [string]$Label, [int[]]$Taken = @())
    $p = $Start
    while (((Test-PortInUse -Port $p) -or ($Taken -contains $p)) -and $p -lt ($Start + 100)) {
        $p++
    }
    if ((Test-PortInUse -Port $p) -or ($Taken -contains $p)) {
        Write-Host "✗ $Label 端口 ${Start}~$($Start + 100) 全被占用，请用环境变量指定" -ForegroundColor Red
        exit 1
    }
    if ($p -ne $Start) {
        Write-Host "  $Label 端口 $Start 被占用，改用 $p" -ForegroundColor Yellow
    }
    return $p
}

function Test-PidAlive {
    param([int]$ProcessId)
    if (-not $ProcessId) { return $false }
    $null = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    return $?
}

function Stop-Pid {
    param([int]$ProcessId, [switch]$Force)
    if (-not $ProcessId) { return }
    try {
        if ($Force) {
            Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
        } else {
            Stop-Process -Id $ProcessId -ErrorAction SilentlyContinue
        }
    } catch {}
}

function Get-PythonExe {
    # 优先使用发行包内捆绑的 Python,实现不依赖目标机系统 Python。
    $bundled = Join-Path $PSScriptRoot 'python\python.exe'
    if (Test-Path $bundled) { return $bundled }

    # 回退:目标机系统 Python(兼容旧包/开发场景)
    $candidates = @(
        'C:\Python314\python.exe',
        'C:\Python313\python.exe',
        'C:\Python312\python.exe',
        'C:\Python311\python.exe'
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) { return 'python' }
    if (Get-Command py -ErrorAction SilentlyContinue)     { return 'py' }
    return $null
}

# ── 0. 关闭本包的现存进程（上次异常退出留下的孤儿） ──
Write-Host ''
Write-Host '[0/5] 清理现存进程...'

# pidfile 记录的进程
$pidFiles = @(Get-ChildItem -Path $PID_DIR -Filter '*.pid' -File -ErrorAction SilentlyContinue)
foreach ($pf in $pidFiles) {
    $pidVal = 0
    if ([int]::TryParse((Get-Content $pf.FullName -Raw).Trim(), [ref]$pidVal)) {
        if (Test-PidAlive -Pid $pidVal) {
            Write-Host "  停止残留 $($pf.BaseName) (PID $pidVal)"
            Stop-Pid -Pid $pidVal
        }
    }
    Remove-Item $pf.FullName -Force -ErrorAction SilentlyContinue
}

# 外部残留进程（跨仓库孤儿也清：上一次 start.ps1/start_3dweb.ps1 从别的包/仓库跑
# 留下的 bridge / sim / competition / redis 会被本包的端口/Redis 抢占或配置串台）。
$allProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
$patterns = @(
    'dist-bridge[\\/]bridge[\\/]index\.js',
    'ts-node.*bridge',                     # 开发环境(start_3dweb)起的 bridge 孤儿
    'webpack serve',                        # 开发环境前端 dev server 孤儿
    'opensim-sim',
    'static-server\.js.*frontend',
    'competition.*run',                     # competition controller (python -m competition run)
    'redis-server.*\.exe'                   # 任何 Redis 实例（下面 §1 会重新起一个）
)
foreach ($pat in $patterns) {
    $matched = $allProcs | Where-Object { $_.CommandLine -and $_.CommandLine -match $pat }
    if ($matched) {
        $pidArr = @()
        foreach ($mm in $matched) { $pidArr += $mm.ProcessId.ToString() }
        $pidList = $pidArr -join ', '
        Write-Host "  清理残留 $pat (PID: $pidList)"
        foreach ($m in $matched) {
            try { Stop-Process -Id $m.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
        }
    }
}

# UE 孤儿进程（按 config/renderers/*.json 的 workdir 清理，跳过 template 与占位符）
$renderersDir = Join-Path $PACK_ROOT 'config\renderers'
if (Test-Path $renderersDir) {
    $renderers = @(Get-ChildItem -Path $renderersDir -Filter '*.json' -File -ErrorAction SilentlyContinue)
    foreach ($r in $renderers) {
        if ($r.Name -like '*.template.json') { continue }
        $workdir = $null
        try {
            # 显式 UTF-8,避免无 BOM 文件被 PS 5.1 按 ANSI 误读(workdir 可能含中文路径)
            $cfg = [System.IO.File]::ReadAllText($r.FullName, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
            $workdir = $cfg.executable.workdir
        } catch {}
        if (-not $workdir) { continue }
        if ($workdir -like '<*>') { continue }  # 占位符跳过

        # Windows 无 /proc，用 CIM 查 CommandLine 匹配 workdir
        $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
        foreach ($p in $procs) {
            if (-not $p.CommandLine) { continue }
            if ($p.CommandLine -match [regex]::Escape($workdir)) {
                Write-Host "  清理 UE 孤儿 (PID $($p.ProcessId), cwd $workdir)"
                try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
            }
        }
    }
}
Start-Sleep -Seconds 1

# ── 环境检测 ──
$python = Get-PythonExe
if (-not $python) {
    Write-Host '✗ 缺少 Python。请先运行: .\setup.ps1' -ForegroundColor Red
    exit 1
}
$pyOk = & $python -c 'import redis, yaml' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '✗ 缺少 Python 依赖（redis/pyyaml）。请先运行: .\setup.ps1' -ForegroundColor Red
    exit 1
}

# ── 端口冲突处理 ──
# Pick-FreePort 的 -Taken 传入此前已分配的端口,保证 REDIS/WS/CAM/CAMWS/WEB
# 五个端口两两不撞(否则 bridge 内多个 listen 会互相 EADDRINUSE,如 CAM_WS 撞 WS
# 时相机画面通道起不来、仿真却照常 —— 症状与根因隔了好几层,极难排查)。
$assigned = @()
$OPENSIM_REDIS_PORT  = Pick-FreePort -Start $OPENSIM_REDIS_PORT  -Label 'REDIS' -Taken $assigned; $assigned += $OPENSIM_REDIS_PORT
$OPENSIM_WS_PORT     = Pick-FreePort -Start $OPENSIM_WS_PORT     -Label 'WS'    -Taken $assigned; $assigned += $OPENSIM_WS_PORT
$OPENSIM_CAM_PORT    = Pick-FreePort -Start $OPENSIM_CAM_PORT    -Label 'CAM'   -Taken $assigned; $assigned += $OPENSIM_CAM_PORT
$OPENSIM_CAM_WS_PORT = Pick-FreePort -Start $OPENSIM_CAM_WS_PORT -Label 'CAMWS' -Taken $assigned; $assigned += $OPENSIM_CAM_WS_PORT
$OPENSIM_WEB_PORT    = Pick-FreePort -Start $OPENSIM_WEB_PORT    -Label 'WEB'   -Taken $assigned; $assigned += $OPENSIM_WEB_PORT

# ── 同步 redis_port 到 scenario.json ──
# 引擎从 scenario.json 读 redis_port，不读环境变量；若不同步，两端在不同 Redis 上。
# 先备份原始 scenario.json，stop.ps1 退出时恢复，避免污染 release 包原始文件。
Write-Host "  同步 redis_port=$OPENSIM_REDIS_PORT 到 scenario.json..."
$backupDir = Join-Path $RUN_DIR 'scenario-backup'
if (-not (Test-Path $backupDir)) { New-Item -ItemType Directory -Path $backupDir -Force | Out-Null }
$scenarioDirs = @(Get-ChildItem -Path (Join-Path $PACK_ROOT 'competition\scenarios') -Directory -ErrorAction SilentlyContinue)
foreach ($sd in $scenarioDirs) {
    $sj = Join-Path $sd.FullName 'scenario.json'
    if (-not (Test-Path $sj)) { continue }
    # 备份原始文件（仅首次，避免覆盖原始备份）
    $backupFile = Join-Path $backupDir "$($sd.Name).scenario.json.bak"
    if (-not (Test-Path $backupFile)) {
        Copy-Item $sj $backupFile -Force
    }
    try {
        # 必须显式按 UTF-8 读取: PS 5.1 的 Get-Content 对无 BOM 文件默认按
        # 系统 ANSI(中文机器=GBK)解码, scenario.json 里的中文注释会被误读,
        # GBK lead byte 还可能吞掉紧随其后的 ASCII 引号导致 ConvertFrom-Json
        # 解析失败(曾致 adversarial_swarm 改写失败、redis_port 残留错位)。
        $cfg = [System.IO.File]::ReadAllText($sj, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
        if (-not $cfg.simulation) {
            $cfg | Add-Member -NotePropertyName 'simulation' -NotePropertyValue ([PSCustomObject]@{})
        }
        $cfg.simulation | Add-Member -NotePropertyName 'redis_port' -NotePropertyValue $OPENSIM_REDIS_PORT -Force
        # 注意: Set-Content -Encoding UTF8 在 PS 5.1 写入 UTF-8 *with BOM*。
        # scenario.json 随后由 Python competition runner 用 json.loads(read_text(
        # encoding="utf-8")) 读取 —— Python 的 "utf-8" 不容忍 BOM,会抛
        # JSONDecodeError,导致 prepared scenario 写成 {} ,引擎报 "no entity"
        # 启动失败(曾引发 sim 黑框 + 无画面回归)。改用 .NET WriteAllText +
        # UTF8Encoding($false) 写无 BOM 的 UTF-8。
        $jsonText = $cfg | ConvertTo-Json -Depth 10
        [System.IO.File]::WriteAllText($sj, $jsonText, (New-Object System.Text.UTF8Encoding($false)))
    } catch {
        # 打印真实异常信息,不要静默吞错(曾因缺此行,编码问题排查多走一轮)
        Write-Host "    ⚠️  改写 $sj 失败（手动检查 redis_port）: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}

# ── 写 run/env.ps1（供 stop.ps1 / verify.ps1 dot-source） ──
$envContent = @"
`$env:OPENSIM_REDIS_PORT = '$OPENSIM_REDIS_PORT'
`$env:OPENSIM_WS_PORT = '$OPENSIM_WS_PORT'
`$env:OPENSIM_CAM_PORT = '$OPENSIM_CAM_PORT'
`$env:OPENSIM_CAM_WS_PORT = '$OPENSIM_CAM_WS_PORT'
`$env:OPENSIM_WEB_PORT = '$OPENSIM_WEB_PORT'
"@
# 用 .NET WriteAllText + UTF8Encoding($false) 写无 BOM,保持仓库约定(见 CLAUDE.md 坑清单#2)
$envPath = Join-Path $RUN_DIR 'env.ps1'
[System.IO.File]::WriteAllText($envPath, $envContent, (New-Object System.Text.UTF8Encoding($false)))

# 进程存活检测（幂等：已起则跳过）
function Test-Alive {
    param([string]$PidFile)
    if (-not (Test-Path $PidFile)) { return $false }
    $pidVal = 0
    if (-not [int]::TryParse((Get-Content $PidFile -Raw).Trim(), [ref]$pidVal)) { return $false }
    return Test-PidAlive -ProcessId $pidVal
}

Write-Host ''
Write-Host '=== Simulation 启动中 ==='

# ── 1. Redis（纯内存模式；redis-windows 不支持 --daemonize，用 Start-Process 后台启动） ──
$redisPidFile = Join-Path $PID_DIR 'redis.pid'
if (-not (Test-Alive -PidFile $redisPidFile)) {
    Write-Host "[1/5] 启动 Redis (port $OPENSIM_REDIS_PORT, 纯内存)..."
    $redisServer = Join-Path $PACK_ROOT 'bin\redis-server.exe'
    $redisCli    = Join-Path $PACK_ROOT 'bin\redis-cli.exe'
    if (-not (Test-Path $redisServer)) {
        Write-Host "✗ Redis 二进制缺失: $redisServer" -ForegroundColor Red
        exit 1
    }
    $redisLog = Join-Path $LOG_DIR 'redis.log'
    $redisDir = Join-Path $RUN_DIR 'redis'
    $redisArgs = @(
        '--port', $OPENSIM_REDIS_PORT.ToString(),
        # --bind 0.0.0.0 --protected-mode no: 多机部署时远程 UE 需跨机连 Redis
        # (内网部署,protected-mode 关闭可接受)。
        '--bind', '0.0.0.0',
        '--protected-mode', 'no',
        '--pidfile', $redisPidFile,
        '--logfile', $redisLog,
        '--dir', $redisDir,
        '--save', '""',
        '--appendonly', 'no'
    )
    $redisProc = Start-Process -FilePath $redisServer -ArgumentList $redisArgs `
        -NoNewWindow -PassThru -RedirectStandardOutput $redisLog -RedirectStandardError "$redisLog.err"
    $redisProc.Id | Out-File -FilePath $redisPidFile -Encoding ASCII -NoNewline

    # 轮询 redis-cli ping 直到 PONG
    $pong = $false
    for ($i = 1; $i -le 20; $i++) {
        $result = & $redisCli -p $OPENSIM_REDIS_PORT ping 2>$null
        if ($result -eq 'PONG') { $pong = $true; break }
        Start-Sleep -Milliseconds 250
    }
    if (-not $pong) {
        Write-Host '✗ Redis 启动失败' -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host '[1/5] Redis 已在运行，跳过'
}

# ── 2. bridge ──
$bridgePidFile = Join-Path $PID_DIR 'bridge.pid'
if (-not (Test-Alive -PidFile $bridgePidFile)) {
    Write-Host "[2/5] 启动 bridge (WS :$OPENSIM_WS_PORT, CAM :$OPENSIM_CAM_PORT, CAMWS :$OPENSIM_CAM_WS_PORT)..."
    $nodeExe = Join-Path $PACK_ROOT 'bin\node.exe'
    $bridgeJs = Join-Path $PACK_ROOT 'visualization\dist-bridge\bridge\index.js'
    if (-not (Test-Path $nodeExe)) {
        Write-Host "✗ Node 二进制缺失: $nodeExe" -ForegroundColor Red
        exit 1
    }
    if (-not (Test-Path $bridgeJs)) {
        Write-Host "✗ bridge 编译产物缺失: $bridgeJs" -ForegroundColor Red
        exit 1
    }
    # 设置环境变量
    $env:NODE_PATH = Join-Path $PACK_ROOT 'lib\node_modules'
    $env:OPENSIM_SIM_BIN = Join-Path $PACK_ROOT 'opensim-sim.exe'
    $env:OPENSIM_RENDERERS_DIR = Join-Path $PACK_ROOT 'config\renderers'
    $env:OPENSIM_SCENARIOS_DIR = Join-Path $PACK_ROOT 'competition\scenarios'
    $env:PYTHON_BIN = $python
    $env:WS_PORT = $OPENSIM_WS_PORT.ToString()
    $env:CAM_HTTP_PORT = $OPENSIM_CAM_PORT.ToString()
    $env:CAM_WS_PORT = $OPENSIM_CAM_WS_PORT.ToString()
    $env:REDIS_HOST = '127.0.0.1'
    $env:REDIS_PORT = $OPENSIM_REDIS_PORT.ToString()
    # OPENSIM_RENDER_CTL_BIN 不设（opensim-render-ctl.exe 缺失，bridge 自动降级）

    $bridgeLog = Join-Path $LOG_DIR 'bridge.log'
    $bridgeErr = Join-Path $LOG_DIR 'bridge.err'
    $bridgeProc = Start-Process -FilePath $nodeExe -ArgumentList $bridgeJs `
        -NoNewWindow -PassThru -RedirectStandardOutput $bridgeLog -RedirectStandardError $bridgeErr
    $bridgeProc.Id | Out-File -FilePath $bridgePidFile -Encoding ASCII -NoNewline

    # 等待 bridge HTTP 端口起来
    $bridgeReady = $false
    for ($i = 1; $i -le 40; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$OPENSIM_CAM_PORT/api/sim/status" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { $bridgeReady = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $bridgeReady) {
        Write-Host '⚠️  bridge HTTP 未就绪（可能仍在启动；查看 run\logs\bridge.log）' -ForegroundColor Yellow
    }
} else {
    Write-Host '[2/5] bridge 已在运行，跳过'
}

# ── 3. 前端静态服务 ──
$frontendPidFile = Join-Path $PID_DIR 'frontend.pid'
if (-not (Test-Alive -PidFile $frontendPidFile)) {
    Write-Host "[3/5] 启动前端静态服务 (:$OPENSIM_WEB_PORT)..."
    $nodeExe = Join-Path $PACK_ROOT 'bin\node.exe'
    $staticSrv = Join-Path $PACK_ROOT 'static-server.js'
    $frontendDir = Join-Path $PACK_ROOT 'frontend'
    $frontendLog = Join-Path $LOG_DIR 'frontend.log'
    $frontendErr = Join-Path $LOG_DIR 'frontend.err'

    $feProc = Start-Process -FilePath $nodeExe -ArgumentList $staticSrv, $frontendDir, $OPENSIM_WEB_PORT.ToString() `
        -NoNewWindow -PassThru -RedirectStandardOutput $frontendLog -RedirectStandardError $frontendErr
    $feProc.Id | Out-File -FilePath $frontendPidFile -Encoding ASCII -NoNewline

    # 等待前端起来
    $feReady = $false
    for ($i = 1; $i -le 20; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$OPENSIM_WEB_PORT/" -UseBasicParsing -TimeoutSec 1 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) { $feReady = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 250
    }
    if (-not $feReady) {
        Write-Host '⚠️  前端未就绪（查看 run\logs\frontend.log）' -ForegroundColor Yellow
    }
} else {
    Write-Host '[3/5] 前端已在运行，跳过'
}

# ── 4. UE 渲染器（仅 UE 版包：检测到 config\renderers\ue_testwl.json 且非 template） ──
# 背景：4ddd4e35 把 UE spawn 从 bridge 移到启动脚本（bridge 改为 Redis 发现模式，
# 只接收 renderer_online，不再 spawn UE 进程）。开发环境 start_3dweb.ps1 已实现，
# 但发版包 release start.ps1 漏了对齐 —— 用户只点「开始仿真」时 bridge 收不到
# renderer_online → 无相机流。本段补齐：读 ue_testwl.json 的 workdir，新 cmd 窗口
# 前台跑 launcher。UE 的 capture_config.json 默认即 service 模式 + 默认地图，故
# 不传任何额外参数（与 start_3dweb.ps1 §3.5 一致，作者已验证）。
#
# 额外（发版包独有）：同步 Redis 端口到 UE capture_config.json。发版包可能因端口
# 占用顺延 $OPENSIM_REDIS_PORT，而 UE 从 capture_config.json 读端口（默认 6379）。
# 不同步 → UE 连旧库、bridge 连新库，永远碰不到（“无相机流”的隐蔽根因之一）。
# 开发环境 Redis 固定 6379 无此问题，故 start_3dweb.ps1 不需要这步。
$uePidFile = Join-Path $PID_DIR 'ue.pid'
$ueStarted = $false
$ueRendererJson = Join-Path $PACK_ROOT 'config\renderers\ue_testwl.json'
if ((Test-Path $ueRendererJson) -and -not $ueRendererJson.EndsWith('.template.json')) {
    $ueWorkdir = $null; $ueLauncher = $null; $ueArgs = @()
    try {
        # -Encoding UTF8：ue_testwl.json 含中文注释，PS 5.1 默认 ANSI(GBK)读会破坏 JSON。
        $ueCfg = Get-Content $ueRendererJson -Raw -Encoding UTF8 | ConvertFrom-Json
        $ueWorkdir = $ueCfg.executable.workdir
        $ueLauncher = $ueCfg.executable.launcher   # Windows: testwl.exe / run.bat
        # args 必须传给 UE！其中 -resx=1 -resy=1 -renderoffscreen 是 UE 不弹可见窗口
        # 的关键（让窗口 1x1 + 离屏）。不传 → UE 走默认全屏/大窗口 → 弹窗抢占前台。
        $ueArgs = @($ueCfg.executable.args)
    } catch {
        Write-Host "  ⚠️  解析 ue_testwl.json 失败：$($_.Exception.Message)" -ForegroundColor Yellow
    }
    # 占位符 workdir（非 UE 包误放了 template 副本）跳过
    if ($ueWorkdir -and $ueWorkdir -notlike '<*>') {
        $ueWorkdirAbs = Join-Path $PACK_ROOT $ueWorkdir
        $ueExe = Join-Path $ueWorkdirAbs $ueLauncher
        # launcher 若是 run.sh（Linux），Windows 下找同名 .bat 或 .exe（与 start_3dweb 一致）
        if (-not (Test-Path $ueExe) -and $ueLauncher -eq 'run.sh') {
            $batAlt = Join-Path $ueWorkdirAbs 'run.bat'
            $exeAlt = Join-Path $ueWorkdirAbs 'testwl.exe'
            if (Test-Path $batAlt) { $ueExe = $batAlt }
            elseif (Test-Path $exeAlt) { $ueExe = $exeAlt }
        }
        if ((Test-Path $ueWorkdirAbs) -and (Test-Path $ueExe)) {
            # 同步 Redis 端口到 UE capture_config.json（发版包端口顺延时必须）。
            # 用 .NET ReadAllText/WriteAllText + UTF8，避免 PS 5.1 GBK 读中文注释乱码。
            # 无条件同步（不加 -ne 6379 守卫）：上次运行若顺延过端口，capture_config
            # 会残留旧端口；本次回到 6379 时必须把它改回来，否则 UE 连旧库 bridge 连
            # 新库 → 无相机流。
            $ueCaptureCfg = Join-Path $ueWorkdirAbs 'testwl\Content\Config\capture_config.json'
            if (Test-Path $ueCaptureCfg) {
                try {
                    $ccText = [System.IO.File]::ReadAllText($ueCaptureCfg, [System.Text.Encoding]::UTF8)
                    $cc = $ccText | ConvertFrom-Json
                    if (-not $cc.redis) { $cc | Add-Member -NotePropertyName redis -NotePropertyValue ([PSCustomObject]@{}) }
                    $cc.redis | Add-Member -NotePropertyName host -NotePropertyValue '127.0.0.1' -Force
                    $cc.redis | Add-Member -NotePropertyName port -NotePropertyValue $OPENSIM_REDIS_PORT -Force
                    $outText = ($cc | ConvertTo-Json -Depth 10)
                    [System.IO.File]::WriteAllText($ueCaptureCfg, $outText, (New-Object System.Text.UTF8Encoding($false)))
                    Write-Host "  同步 UE capture_config.json redis_port=$OPENSIM_REDIS_PORT"
                } catch {
                    Write-Host "  ⚠️  同步 UE capture_config.json 失败：$($_.Exception.Message)" -ForegroundColor Yellow
                }
            }

            Write-Host "[4/5] 启动 UE 渲染器 (service 模式, 后台无窗口, workdir $ueWorkdir)..."
            # 后台隐藏窗口跑 UE（不弹窗，不抢占前台）。UE 的 stdout/stderr 重定向到
            # run/logs/ue.log —— 含 [Perf] FPS=/Frame= 帧率与调试信息，用
            # `Get-Content run\logs\ue.log -Wait` 实时查看。
            # 已验证：-WindowStyle Hidden 下 UE 仍正常渲染（GPU 上下文不依赖可见窗口）、
            # 照常连 Redis 发 renderer_online、stdout 完整落盘。
            # UE 首次加载地图需数分钟，之后自动 idle 待命（service 模式设计）。
            $ueLog = Join-Path $LOG_DIR 'ue.log'
            $ueErr = Join-Path $LOG_DIR 'ue.err'
            # 组装 UE 启动参数：地图(UE 命令行第一个位置参数) + ue_testwl.json 的 args。
            # args 里的 -resx=1 -resy=1 -renderoffscreen 让 UE 创建 1x1 离屏窗口而非
            # 可见窗口；配合 -WindowStyle Hidden 双保险，UE 完全不弹窗也不抢占前台。
            $ueFinalArgs = New-Object System.Collections.Generic.List[string]
            $ueFinalArgs.Add('/Env_MultiBS_Data/Maps/Map_MultiBS.Map_MultiBS')  # 默认地图(UE 项目决定)
            foreach ($a in $ueArgs) { $ueFinalArgs.Add($a) }
            $ueProc = Start-Process -FilePath $ueExe `
                -ArgumentList $ueFinalArgs `
                -WorkingDirectory $ueWorkdirAbs `
                -WindowStyle Hidden `
                -RedirectStandardOutput $ueLog `
                -RedirectStandardError $ueErr `
                -PassThru
            # UE launcher 的 PID；stop.ps1 用 taskkill /T 带走 launcher→Shipping 子进程树。
            $ueProc.Id | Out-File -FilePath $uePidFile -Encoding ASCII -NoNewline
            Write-Host "  本地 UE 已后台启动 (PID $($ueProc.Id), 无窗口)"
            Write-Host '  首次加载地图需数分钟；加载完点「开始仿真」即有相机画面'
            Write-Host "  看帧率/调试: Get-Content $LOG_DIR\ue.log -Wait"
            Write-Host '  停止: .\stop.ps1 (UE 随 bridge 一起停)'
            $ueStarted = $true
        } else {
            Write-Host "[4/5] ue_testwl.json workdir 无效或无 launcher，本地 UE 需手动启动: $ueWorkdir" -ForegroundColor Yellow
        }
    } else {
        Write-Host '[4/5] 非 UE 版包，跳过 UE 启动'
    }
} else {
    Write-Host '[4/5] 非 UE 版包，跳过 UE 启动'
}

Write-Host ''
Write-Host '=========================================='
Write-Host '  Simulation 已启动'
Write-Host '=========================================='
Write-Host "  浏览器访问: http://localhost:$OPENSIM_WEB_PORT"
Write-Host '  选赛题 → 「算法」框可填 module:Class（留空用 baseline）→ 点「开始仿真」'
if ($ueStarted) {
    Write-Host '  UE 渲染器已后台启动（无窗口，首次加载需数分钟）'
    Write-Host "  看 UE 帧率/调试: Get-Content $LOG_DIR\ue.log -Wait"
}
Write-Host ''

# 自动打开浏览器（设 OPENSIM_NO_OPEN_BROWSER=1 可禁用）
if ($env:OPENSIM_NO_OPEN_BROWSER -ne '1') {
    $url = "http://localhost:$OPENSIM_WEB_PORT"
    try {
        Start-Process $url
        Write-Host "  已尝试打开浏览器：$url"
    } catch {
        Write-Host "  ⚠️  无法打开浏览器，请手动访问 $url" -ForegroundColor Yellow
    }
}
Write-Host ''
Write-Host "  日志: Get-Content $LOG_DIR\bridge.log -Wait"
Write-Host '  停止: .\stop.ps1'
Write-Host '  检查: .\verify.ps1'
Write-Host '=========================================='
