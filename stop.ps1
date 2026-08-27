# stop.ps1 — 停止 Simulation release 包所有进程（Windows 版）
# 顺序: 前端 → bridge → competition/sim → UE 孤儿 → redis
# 含 UE 孤儿进程兜底清理（复刻 stop.sh 的 kill_renderer_workdir_procs 等价逻辑）。

#Requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'  # stop.sh 用 set -u，单个失败继续

# 推导包根
$PACK_ROOT = $PSScriptRoot
$PID_DIR = Join-Path $PACK_ROOT 'run\pids'

# 读取 start.ps1 写入的实际端口
$envFile = Join-Path $PACK_ROOT 'run\env.ps1'
if (Test-Path $envFile) {
    . $envFile
}
$OPENSIM_REDIS_PORT = if ($env:OPENSIM_REDIS_PORT) { [int]$env:OPENSIM_REDIS_PORT } else { 6379 }

# ── 工具函数 ──
function Stop-Pidfile {
    param([string]$Name, [string]$PidFile)
    if (-not (Test-Path $PidFile)) { return }
    $pidVal = 0
    if (-not [int]::TryParse((Get-Content $PidFile -Raw).Trim(), [ref]$pidVal)) {
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
        return
    }
    $proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
    if ($proc) {
        # Windows 无 SIGTERM/SIGKILL 区分，先 taskkill /T 让进程树优雅退出，等 3 秒，再 /F
        try { taskkill /PID $pidVal /T 2>$null | Out-Null } catch {}
        for ($i = 1; $i -le 3; $i++) {
            Start-Sleep -Seconds 1
            $stillAlive = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
            if (-not $stillAlive) { break }
        }
        $stillAlive = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
        if ($stillAlive) {
            try { taskkill /PID $pidVal /T /F 2>$null | Out-Null } catch {}
            Write-Host "    (taskkill /F 兜底)"
        }
        Write-Host "  ✓ 停止 $Name (PID $pidVal)"
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

Write-Host '=== 停止 Simulation 进程 ==='

# 0. 恢复 scenario.json 原始备份（start.ps1 修改前备份的）
$backupDir = Join-Path $PACK_ROOT 'run\scenario-backup'
if (Test-Path $backupDir) {
    Get-ChildItem -Path $backupDir -Filter '*.scenario.json.bak' -ErrorAction SilentlyContinue | ForEach-Object {
        $scenarioName = [System.IO.Path]::GetFileNameWithoutExtension($_.Name).Replace('.scenario.json', '')
        $target = Join-Path $PACK_ROOT "competition\scenarios\$scenarioName\scenario.json"
        if (Test-Path $target) {
            Copy-Item $_.FullName $target -Force
        }
    }
    Remove-Item $backupDir -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host '  ✓ 恢复 scenario.json 原始备份'
}

# 1. 前端
Stop-Pidfile -Name '前端静态服务' -PidFile (Join-Path $PID_DIR 'frontend.pid')

# 2. bridge（会带走其子进程 competition → opensim-sim）
Stop-Pidfile -Name 'bridge' -PidFile (Join-Path $PID_DIR 'bridge.pid')

# 3. competition / opensim-sim 残留兜底
$allProcs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue
$residualPatterns = @('competition run', 'opensim-sim')
foreach ($pat in $residualPatterns) {
    $matched = $allProcs | Where-Object { $_.CommandLine -and $_.CommandLine -match $pat }
    if ($matched) {
        $pidList = ($matched | ForEach-Object { $_.ProcessId.ToString() }) -join ', '
        Write-Host "  清理残留 $pat (PID: $pidList)"
        foreach ($m in $matched) {
            try { taskkill /PID $m.ProcessId /T 2>$null | Out-Null } catch {}
        }
        Start-Sleep -Seconds 1
        foreach ($m in $matched) {
            $stillAlive = Get-Process -Id $m.ProcessId -ErrorAction SilentlyContinue
            if ($stillAlive) {
                try { taskkill /PID $m.ProcessId /T /F 2>$null | Out-Null } catch {}
            }
        }
    }
}

# 4. UE 渲染器（start.ps1 启动的，按 ue.pid；taskkill /T 带走 launcher→Shipping 子进程树）
Stop-Pidfile -Name 'UE 渲染器' -PidFile (Join-Path $PID_DIR 'ue.pid')

# 4b. UE 孤儿进程兜底（按 config/renderers/*.json 的 workdir 清理）
$renderersDir = Join-Path $PACK_ROOT 'config\renderers'
if (Test-Path $renderersDir) {
    $renderers = @(Get-ChildItem -Path $renderersDir -Filter '*.json' -File -ErrorAction SilentlyContinue)
    foreach ($r in $renderers) {
        if ($r.Name -like '*.template.json') { continue }
        $workdir = $null
        try {
            $cfg = Get-Content $r.FullName -Raw | ConvertFrom-Json
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

# 5. Redis（最后停）—— 不依赖 pidfile，直接 redis-cli shutdown
$redisStopped = $false
$redisCli = Join-Path $PACK_ROOT 'bin\redis-cli.exe'
if (Test-Path $redisCli) {
    try {
        & $redisCli -p $OPENSIM_REDIS_PORT shutdown nosave 2>$null
        if ($LASTEXITCODE -eq 0) { $redisStopped = $true }
    } catch {}
}

# pidfile 兜底
if (-not $redisStopped) {
    $redisPidFile = Join-Path $PID_DIR 'redis.pid'
    if (Test-Path $redisPidFile) {
        $pidVal = 0
        if ([int]::TryParse((Get-Content $redisPidFile -Raw).Trim(), [ref]$pidVal)) {
            $proc = Get-Process -Id $pidVal -ErrorAction SilentlyContinue
            if ($proc) {
                try { taskkill /PID $pidVal /T /F 2>$null | Out-Null } catch {}
                $redisStopped = $true
            }
        }
        Remove-Item $redisPidFile -Force -ErrorAction SilentlyContinue
    }
}

# 最终兜底：按端口找 PID（用 Get-NetTCPConnection）
if (-not $redisStopped) {
    try {
        $conns = Get-NetTCPConnection -LocalPort $OPENSIM_REDIS_PORT -State Listen -ErrorAction Stop
        $rpid = ($conns | Select-Object -First 1).OwningProcess
        if ($rpid) {
            try { taskkill /PID $rpid /T /F 2>$null | Out-Null } catch {}
            $redisStopped = $true
        }
    } catch {}
}

Remove-Item -Path (Join-Path $PID_DIR 'redis.pid') -Force -ErrorAction SilentlyContinue
if ($redisStopped) {
    Write-Host "  ✓ 停止 redis (port $OPENSIM_REDIS_PORT)"
} else {
    Write-Host '  redis 未在运行（无需停止）'
}

Write-Host '=== 停止完成 ==='
