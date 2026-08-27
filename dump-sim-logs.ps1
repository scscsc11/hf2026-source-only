# dump-sim-logs.ps1 - dump the two critical error logs after a stalled sim
# Usage: cd to package root, then run this script
# It will:
#   1. wait for you to click "Start Simulation" and see the stall
#   2. wait for the Start button to re-enable (sim crashed)
#   3. dump the newest sim.stderr.log / controller.stderr.log
# NOTE: bridge 把引擎/控制器日志写到 competition\scenarios\<赛题>\output\，
#       不是 run\sim-output\（旧文档描述已过时）。
# Output: a single combined file at run/logs/dump-<timestamp>.log

#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$PACK_ROOT  = (Resolve-Path $PSScriptRoot).Path
$SCEN_DIR   = Join-Path $PACK_ROOT 'competition\scenarios'
$LEGACY_OUT = Join-Path $PACK_ROOT 'run\sim-output'
$LOG_DIR    = Join-Path $PACK_ROOT 'run\logs'
$STAMP      = Get-Date -Format 'yyyyMMdd-HHmmss'
$DUMP       = Join-Path $LOG_DIR ("dump-$STAMP.log")

if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null }

function Out {
    param([string]$Msg)
    Write-Host $Msg
    Add-Content -Path $DUMP -Value $Msg
}

# 输出日志全文；超过 1MB 只保留最后 500 行
function Out-LogFile {
    param([string]$Path, [string]$Label)
    $size = (Get-Item $Path).Length
    Out "  file: $Path"
    Out "  file size: $size bytes"
    if ($size -eq 0) {
        Out "  (empty - 进程在写出任何内容之前就死了)"
        return
    }
    Out "  ---------- BEGIN $Label ----------"
    if ($size -gt 1MB) {
        Out "  [truncated: 原始大小 $([math]::Round($size / 1MB, 1)) MB，仅保留最后 500 行]"
        Get-Content $Path -Tail 500 -ErrorAction SilentlyContinue | ForEach-Object { Out ("  " + $_) }
    } else {
        Get-Content $Path -ErrorAction SilentlyContinue | ForEach-Object { Out ("  " + $_) }
    }
    Out "  ---------- END $Label ----------"
}

Out "===== dump-sim-logs start: $STAMP ====="
Out "package root: $PACK_ROOT"
Out ""

Out "## STEP 1: In your browser, click 'Start Simulation'."
Out "## STEP 2: Wait until progress bar stalls (e.g. 5%) AND the button re-enables."
Out "##         That signals competition runner has crashed."
Out "## STEP 3: Press Enter here."
Out ""
Read-Host "Press Enter once the Start button has re-enabled"

# Wait a couple seconds so any buffered stderr is fully flushed
Start-Sleep -Seconds 2

# ---- 各赛题 output 目录清单 ----
Out ""
Out "===== competition\scenarios\*\output\ directory listing ====="
$scenarioOutputs = Get-ChildItem (Join-Path $SCEN_DIR '*\output') -Directory -ErrorAction SilentlyContinue
if ($scenarioOutputs) {
    foreach ($od in $scenarioOutputs) {
        Out "-- $($od.Parent.Name)\output\ --"
        Get-ChildItem $od -ErrorAction SilentlyContinue | ForEach-Object {
            Out ("  {0,12}  {1:yyyy-MM-dd HH:mm:ss}  {2}" -f $_.Length, $_.LastWriteTime, $_.Name)
        }
    }
} else {
    Out "  (没有任何赛题 output 目录 —— 可能从未成功启动过仿真)"
}

# ---- 旧的 run\sim-output\（历史位置，一般应为空；有内容也一并列出）----
if (Test-Path $LEGACY_OUT) {
    $legacyFiles = Get-ChildItem $LEGACY_OUT -File -ErrorAction SilentlyContinue
    if ($legacyFiles) {
        Out ""
        Out "===== run\sim-output\ (legacy) directory listing ====="
        $legacyFiles | ForEach-Object {
            Out ("  {0,12}  {1:yyyy-MM-dd HH:mm:ss}  {2}" -f $_.Length, $_.LastWriteTime, $_.Name)
        }
    }
}

# ---- 找最新的引擎 / 控制器日志（跨所有赛题）----
$simErrs  = @(Get-ChildItem (Join-Path $SCEN_DIR '*\output\sim.stderr.log')        -ErrorAction SilentlyContinue)
$ctrlErrs = @(Get-ChildItem (Join-Path $SCEN_DIR '*\output\controller.stderr.log')  -ErrorAction SilentlyContinue)
# 兼容旧位置
if (-not $simErrs  -and (Test-Path (Join-Path $LEGACY_OUT 'sim.stderr.log')))        { $simErrs  = @(Get-Item (Join-Path $LEGACY_OUT 'sim.stderr.log')) }
if (-not $ctrlErrs -and (Test-Path (Join-Path $LEGACY_OUT 'controller.stderr.log'))) { $ctrlErrs = @(Get-Item (Join-Path $LEGACY_OUT 'controller.stderr.log')) }

Out ""
Out "===== sim.stderr.log (engine stderr - PRIMARY source of truth) ====="
if ($simErrs.Count) {
    $newestSim = $simErrs | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Out-LogFile $newestSim.FullName 'sim.stderr.log'
    if ($newestSim.Length -eq 0) {
        Out "  !! sim.stderr.log 为 0 字节 = 引擎在 main() 之前崩溃（多为缺 DLL / VC++ Redistributable / 被杀软拦截）"
        Out "  !! 建议再运行 diagnose.ps1 收集完整诊断信息"
    }
} else {
    Out "  (任何赛题下都没有 sim.stderr.log)"
}

Out ""
Out "===== controller.stderr.log (python competition runner stderr) ====="
if ($ctrlErrs.Count) {
    $newestCtrl = $ctrlErrs | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    Out-LogFile $newestCtrl.FullName 'controller.stderr.log'
} else {
    Out "  (任何赛题下都没有 controller.stderr.log)"
}

Out ""
Out "===== run\logs\ (latest 20 entries) ====="
Get-ChildItem $LOG_DIR -Filter '*.log' -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 20 |
    ForEach-Object {
        Out ("  {0,12}  {1:yyyy-MM-dd HH:mm:ss}  {2}" -f $_.Length, $_.LastWriteTime, $_.Name)
    }

Out ""
Out "===== dump-sim-logs done ====="
Out "Please send run\logs\$((Split-Path $DUMP -Leaf)) to the developer."
Out "(或直接运行 diagnose.ps1，把生成的 opensim-diagnostics-*.zip 发回，信息更全)"
