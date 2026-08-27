# diagnose.ps1 — Simulation 一键诊断打包（Windows 版）
#
# 用途：在出问题的用户机器上运行，自动收集系统信息、包完整性、运行依赖、
#       端口/进程状态、全部日志与崩溃痕迹，打包成一个 zip。
# 用法：在发布包根目录执行（推荐右键 "使用 PowerShell 运行"），或：
#       powershell -ExecutionPolicy Bypass -File .\diagnose.ps1
# 产出：包根目录下 simulation-diagnostics-<时间戳>.zip —— 请把该 zip 发回运维人员。
#
# 本脚本只读不写（除诊断目录外），不会影响正在运行的服务，可随时重复执行。

#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$NoZip   # 只生成诊断目录，不打 zip（调试用）
)

$ErrorActionPreference = 'Continue'

$PACK_ROOT = $PSScriptRoot
$ts = Get-Date -Format 'yyyyMMdd-HHmmss'

# ---- 诊断输出目录（优先包内 run\logs\diagnostics，不可写则退回 %TEMP%） ----
$diagParent = Join-Path $PACK_ROOT 'run\logs\diagnostics'
try {
    New-Item -ItemType Directory -Force -Path $diagParent | Out-Null
    $probe = Join-Path $diagParent ".probe-$ts"
    New-Item -ItemType File -Force -Path $probe | Out-Null
    Remove-Item $probe -Force
} catch {
    $diagParent = Join-Path $env:TEMP 'simulation-diagnostics'
    New-Item -ItemType Directory -Force -Path $diagParent | Out-Null
}
$DIR = Join-Path $diagParent "simulation-diagnostics-$ts"
New-Item -ItemType Directory -Force -Path $DIR | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DIR 'logs') | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $DIR 'sim-output') | Out-Null

# ---- 检查结果登记（最后汇总进 00-summary.txt） ----
$script:Results = @()
function Add-Check {
    param([string]$Status, [string]$Item, [string]$Hint = '')
    $script:Results += [pscustomobject]@{ Status = $Status; Item = $Item; Hint = $Hint }
    $color = switch ($Status) { 'PASS' { 'Green' } 'WARN' { 'Yellow' } default { 'Red' } }
    Write-Host "  [$Status] $Item" -ForegroundColor $color
    if ($Hint) { Write-Host "         -> $Hint" -ForegroundColor DarkYellow }
}

function W {
    param([string]$File, [string]$Text = '')
    Add-Content -Path $File -Value $Text -Encoding UTF8
}

# 采集一段命令输出到指定文件，永不因单步失败中断
function Capture {
    param([string]$File, [string]$Title, [scriptblock]$Body)
    W $File "=== $Title ==="
    try {
        $out = & $Body 2>&1 | Out-String -Width 300
        if ($out.Trim()) { W $File $out.TrimEnd() } else { W $File '(无输出)' }
    } catch {
        W $File "(采集失败: $($_.Exception.Message))"
    }
    W $File ''
}

# 拷贝日志，超过 2MB 只保留最后 2000 行；返回是否拷到
function Copy-LogFile {
    param([string]$Src, [string]$DestDir, [string]$Prefix = '')
    if (-not (Test-Path $Src -PathType Leaf)) { return $false }
    $item = Get-Item $Src
    $dest = Join-Path $DestDir ($Prefix + $item.Name)
    if ($item.Length -gt 2MB) {
        W $dest "[截断] 原始大小 $([math]::Round($item.Length / 1MB, 1)) MB，仅保留最后 2000 行"
        Get-Content $Src -Tail 2000 | Add-Content $dest -Encoding UTF8
    } else {
        Copy-Item $Src $dest -Force
    }
    return $true
}

Write-Host ''
Write-Host '=== Simulation 一键诊断 ===' -ForegroundColor Cyan
Write-Host "包根目录: $PACK_ROOT"
Write-Host "诊断目录: $DIR"
Write-Host ''

# ======================================================================
# 01 系统信息
# ======================================================================
Write-Host '[1/8] 收集系统信息...'
$f = Join-Path $DIR '01-system.txt'
W $f "采集时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
W $f "包根目录: $PACK_ROOT"
W $f "路径长度: $($PACK_ROOT.Length) 字符"
if ($PACK_ROOT.Length -gt 120) {
    Add-Check 'WARN' "包路径较长（$($PACK_ROOT.Length) 字符）" '超过 200 字符可能触发 MAX_PATH 问题，建议把包挪到更浅目录（如 D:\simulation）'
}
Capture $f '操作系统' {
    $os = Get-CimInstance Win32_OperatingSystem
    "版本: $($os.Caption) $($os.Version) (Build $($os.BuildNumber))"
    "架构: $env:PROCESSOR_ARCHITECTURE"
    "主机名: $env:COMPUTERNAME"
    "上次启动: $($os.LastBootUpTime)"
}
if ((Get-CimInstance Win32_OperatingSystem).BuildNumber -lt 17763) {
    Add-Check 'WARN' 'Windows 版本低于 10 1809' '官方支持最低 Windows 10 1809 (Build 17763)'
}
Capture $f '硬件资源' {
    $cs = Get-CimInstance Win32_ComputerSystem
    $os = Get-CimInstance Win32_OperatingSystem
    "CPU 逻辑核数: $env:NUMBER_OF_PROCESSORS"
    "内存总量: $([math]::Round($cs.TotalPhysicalMemory / 1GB, 1)) GB"
    "内存可用: $([math]::Round($os.FreePhysicalMemory / 1MB, 1)) GB"
    $drive = $PACK_ROOT.Substring(0, 2)
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$drive'"
    "磁盘 $drive 可用: $([math]::Round($disk.FreeSpace / 1GB, 1)) GB / 总计 $([math]::Round($disk.Size / 1GB, 1)) GB"
}
Capture $f 'PowerShell / 权限' {
    "PowerShell: $($PSVersionTable.PSVersion)"
    "执行策略: $(Get-ExecutionPolicy)"
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    "是否管理员: $([Security.Principal.WindowsPrincipal]::new($id).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))"
}
Capture $f 'VERSION 文件' {
    $vf = Join-Path $PACK_ROOT 'VERSION'
    if (Test-Path $vf) { Get-Content $vf } else { '(VERSION 文件缺失)' }
}

# ======================================================================
# 02 环境变量
# ======================================================================
Write-Host '[2/8] 收集环境变量...'
$f = Join-Path $DIR '02-environment.txt'
Capture $f 'Simulation 相关环境变量' {
    Get-ChildItem env: | Where-Object {
        $_.Name -match '^(OPENSIM_|REDIS_|WS_PORT|CAM_|PYTHON_BIN|NODE_PATH|UE_|MINGW_)'
    } | Sort-Object Name | ForEach-Object { "$($_.Name) = $($_.Value)" }
}
Capture $f 'PATH' { $env:PATH -split ';' }

# ======================================================================
# 03 包完整性检查
# ======================================================================
Write-Host '[3/8] 检查包完整性...'
$f = Join-Path $DIR '03-package-integrity.txt'
$required = @(
    @{ Path = 'opensim-sim.exe';            Desc = '仿真引擎';           Critical = $true },
    @{ Path = 'libwinpthread-1.dll';        Desc = 'MinGW 运行时';       Critical = $true },
    @{ Path = 'libgcc_s_seh-1.dll';         Desc = 'MinGW 运行时';       Critical = $true },
    @{ Path = 'libstdc++-6.dll';            Desc = 'MinGW 运行时';       Critical = $true },
    @{ Path = 'bin\node.exe';               Desc = 'Node.js 运行时';     Critical = $true },
    @{ Path = 'bin\redis-server.exe';       Desc = 'Redis 服务';         Critical = $true },
    @{ Path = 'bin\redis-cli.exe';          Desc = 'Redis 客户端';       Critical = $true },
    @{ Path = 'bin\msys-2.0.dll';           Desc = 'MSYS2 运行时(Redis)'; Critical = $true },
    @{ Path = 'python\python.exe';          Desc = '捆绑 Python';        Critical = $true },
    @{ Path = 'visualization\dist-bridge\bridge\index.js'; Desc = 'bridge 服务'; Critical = $true },
    @{ Path = 'frontend\index.html';        Desc = '前端页面';           Critical = $true },
    @{ Path = 'frontend\bundle.js';         Desc = '前端 bundle';        Critical = $true },
    @{ Path = 'frontend\heightmap.json';    Desc = '前端地形数据';       Critical = $false },
    @{ Path = 'config\HeightSample.csv';    Desc = '地形高程数据';       Critical = $true },
    @{ Path = 'config\GridDataAll_18.csv';  Desc = '地形网格数据';       Critical = $true },
    @{ Path = 'config\terrain_bbox.json';   Desc = '地形包围盒缓存';     Critical = $false },
    @{ Path = 'config\points.json';         Desc = '航点数据';           Critical = $true },
    @{ Path = 'config\random_routes_20.json'; Desc = '随机路线池';       Critical = $true },
    @{ Path = 'config\defaults.json';       Desc = '默认配置';           Critical = $true },
    @{ Path = 'competition\__main__.py';    Desc = 'competition 入口';   Critical = $true },
    @{ Path = 'competition\sdk\core\runner.py'; Desc = 'competition runner'; Critical = $true }
)
W $f '文件清单（缺失项同时会写入 00-summary.txt）：'
foreach ($r in $required) {
    $full = Join-Path $PACK_ROOT $r.Path
    if (Test-Path $full -PathType Leaf) {
        $size = (Get-Item $full).Length
        W $f ("  [存在] {0,-45} {1,12:N0} bytes" -f $r.Path, $size)
    } else {
        W $f "  [缺失] $($r.Path)   <-- $($r.Desc)"
        if ($r.Critical) {
            Add-Check 'FAIL' "缺少文件: $($r.Path)（$($r.Desc)）" '发布包不完整（解压中断/杀毒软件误删），请重新解压完整发布包；若反复被删请检查杀毒软件隔离区并加白名单'
        } else {
            Add-Check 'WARN' "缺少文件: $($r.Path)（$($r.Desc)）" '非致命，但可能影响启动速度或前端显示'
        }
    }
}
# 目录级检查
foreach ($d in @('config\models', 'competition\scenarios', 'competition\sdk', 'lib\node_modules')) {
    $full = Join-Path $PACK_ROOT $d
    $n = 0
    if (Test-Path $full) { $n = (Get-ChildItem $full -Recurse -File -ErrorAction SilentlyContinue | Measure-Object).Count }
    W $f "  [目录] $d  ($n 个文件)"
    if ($n -eq 0) { Add-Check 'FAIL' "目录为空或缺失: $d" '发布包不完整，请重新解压' }
}
Add-Check 'PASS' '包完整性检查完成（详见 03-package-integrity.txt）'

# ======================================================================
# 04 运行依赖检查
# ======================================================================
Write-Host '[4/8] 检查运行依赖...'
$f = Join-Path $DIR '04-runtime-deps.txt'

# 4.1 VC++ Redistributable（注册表）
Capture $f 'VC++ 2015-2022 Redistributable (x64) 注册表检测' {
    $keys = @(
        'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64'
    )
    $found = $false
    foreach ($k in $keys) {
        if (Test-Path $k) {
            $p = Get-ItemProperty $k
            "$k -> Installed=$($p.Installed) Version=$($p.Version)"
            if ($p.Installed -eq 1) { $found = $true }
        }
    }
    if (-not $found) { '未检测到 VC++ Redistributable (x64)' }
}
$vcInstalled = $false
foreach ($k in @('HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
                 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64')) {
    if (Test-Path $k) {
        $p = Get-ItemProperty $k -ErrorAction SilentlyContinue
        if ($p -and $p.Installed -eq 1) { $vcInstalled = $true }
    }
}
if ($vcInstalled) {
    Add-Check 'PASS' 'VC++ 2015-2022 Redistributable (x64) 已安装'
} else {
    Add-Check 'FAIL' '未安装 VC++ 2015-2022 Redistributable (x64)' '捆绑 Python 无法启动。请运行包内 setup.ps1（会自动静默安装 vc_redist.x64.exe），安装后建议重启系统'
}

# 4.2 系统关键 DLL
Capture $f 'System32 关键 DLL' {
    foreach ($dll in @('vcruntime140.dll', 'vcruntime140_1.dll', 'msvcp140.dll')) {
        $p = Join-Path "$env:SystemRoot\System32" $dll
        if (Test-Path $p) { "  [存在] $dll" } else { "  [缺失] $dll" }
    }
}

# 4.3 Python：优先捆绑 python，退回系统 python
$pythonBin = $null
$bundled = Join-Path $PACK_ROOT 'python\python.exe'
if (Test-Path $bundled) {
    $pythonBin = $bundled
} else {
    foreach ($c in @('C:\Python314\python.exe', 'C:\Python313\python.exe', 'C:\Python312\python.exe', 'C:\Python311\python.exe')) {
        if (Test-Path $c) { $pythonBin = $c; break }
    }
    if (-not $pythonBin -and (Get-Command python -ErrorAction SilentlyContinue)) { $pythonBin = 'python' }
    if (-not $pythonBin -and (Get-Command py -ErrorAction SilentlyContinue)) { $pythonBin = 'py' }
}
W $f "=== Python 检测 ==="
if ($pythonBin) {
    W $f "使用: $pythonBin"
    Capture $f 'python --version' { & $pythonBin --version }
    Capture $f 'import redis / yaml' {
        & $pythonBin -c 'import redis, yaml; print("redis", redis.__version__); print("yaml OK")'
    }
    & $pythonBin -c 'import redis, yaml' 2>$null
    if ($LASTEXITCODE -eq 0) {
        Add-Check 'PASS' "Python 依赖 redis/pyyaml 就绪（$pythonBin）"
    } else {
        Add-Check 'FAIL' "Python 缺少 redis/pyyaml（$pythonBin）" '请运行包内 setup.ps1；或手动执行 python -m pip install redis pyyaml'
    }
} else {
    W $f '未找到任何 Python'
    Add-Check 'FAIL' '未找到 Python' '发布包应自带 python\ 目录；若缺失说明解压不完整，请重新解压'
}

# 4.4 Redis 冒烟
$redisServer = Join-Path $PACK_ROOT 'bin\redis-server.exe'
if (Test-Path $redisServer) {
    Capture $f 'redis-server --version' { & $redisServer --version }
} else {
    W $f "`n=== redis-server ===`n(缺失)"
}

# 4.5 scenario.json BOM / 解析检查 + 4.6 .pyc magic 检查（有 python 时一次做完）
if ($pythonBin) {
    Capture $f 'scenario.json BOM / JSON 解析检查' {
        & $pythonBin -c @"
import json, pathlib
root = pathlib.Path(r'$($PACK_ROOT -replace '\\', '/')')
for p in sorted(root.glob('competition/scenarios/*/scenario.json')):
    raw = p.read_bytes()
    bom = raw[:3] == b'\xef\xbb\xbf'
    try:
        json.loads(raw.decode('utf-8-sig'))
        st = 'OK'
    except Exception as e:
        st = 'PARSE_FAIL: %s' % e
    print('%s: bom=%s %s' % (p.parent.name, bom, st))
"@
    }
    Capture $f '.pyc magic 版本一致性' {
        & $pythonBin -c @"
import importlib.util, pathlib
magic = importlib.util.MAGIC_NUMBER
bad = []
root = pathlib.Path(r'$($PACK_ROOT -replace '\\', '/')')
for p in root.glob('competition/**/*.pyc'):
    with open(p, 'rb') as fh:
        if fh.read(4) != magic:
            bad.append(str(p))
print('当前解释器 magic:', magic.hex())
print('不匹配的 .pyc 数量:', len(bad))
for b in bad[:20]:
    print(' ', b)
"@
    }
} else {
    # 无 python 时的退化检查（纯 PowerShell）
    Capture $f 'scenario.json BOM 检查（无 Python，仅查 BOM）' {
        Get-ChildItem (Join-Path $PACK_ROOT 'competition\scenarios\*\scenario.json') -ErrorAction SilentlyContinue | ForEach-Object {
            $bytes = [IO.File]::ReadAllBytes($_.FullName)[0..2]
            $bom = ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
            "$($_.Directory.Name): bom=$bom"
        }
    }
}

# ======================================================================
# 05 端口与进程
# ======================================================================
Write-Host '[5/8] 检查端口与进程...'
$f = Join-Path $DIR '05-ports-processes.txt'

# 读取 start.ps1 记录的实际端口
$envFile = Join-Path $PACK_ROOT 'run\env.ps1'
if (Test-Path $envFile) {
    W $f "=== run\env.ps1（上次启动实际使用的端口）==="
    W $f (Get-Content $envFile -Raw)
    . $envFile
} else {
    W $f '(run\env.ps1 不存在 —— start.ps1 可能从未成功执行到写端口那一步)'
}
$ports = @(
    @{ Name = 'Redis';    Port = $(if ($env:OPENSIM_REDIS_PORT) { [int]$env:OPENSIM_REDIS_PORT } else { 6379 }) },
    @{ Name = 'bridge-WS'; Port = $(if ($env:OPENSIM_WS_PORT)  { [int]$env:OPENSIM_WS_PORT }  else { 8080 }) },
    @{ Name = 'bridge-HTTP'; Port = $(if ($env:OPENSIM_CAM_PORT) { [int]$env:OPENSIM_CAM_PORT } else { 8081 }) },
    @{ Name = '前端 Web';  Port = $(if ($env:OPENSIM_WEB_PORT) { [int]$env:OPENSIM_WEB_PORT }  else { 3000 }) }
)
W $f "`n=== 端口监听状态 ==="
foreach ($p in $ports) {
    $conn = Get-NetTCPConnection -State Listen -LocalPort $p.Port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
        $procName = if ($proc) { $proc.ProcessName } else { '未知' }
        W $f ("  {0,-12} 端口 {1,-6} [监听中] 占用进程: {2} (PID {3})" -f $p.Name, $p.Port, $procName, $conn.OwningProcess)
    } else {
        W $f ("  {0,-12} 端口 {1,-6} [未监听]" -f $p.Name, $p.Port)
    }
}

W $f "`n=== Simulation 相关进程（含可疑孤儿进程）==="
$patterns = 'opensim-sim|opensim-render|redis-server|node|python'
$procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match $patterns -and $_.CommandLine -match 'opensim|redis|dist-bridge|static-server|competition'
}
if ($procs) {
    foreach ($p in $procs) {
        W $f ("  PID {0,-7} {1,-18} 启动于 {2}" -f $p.ProcessId, $p.Name, $p.CreationDate)
        W $f ("           cmd: {0}" -f ($p.CommandLine -replace "`r?`n", ' '))
    }
} else {
    W $f '  (无 Simulation 相关进程在运行)'
}
# 多个 opensim-sim 并存 = 孤儿进程
$simCount = @($procs | Where-Object { $_.Name -match 'opensim-sim' }).Count
if ($simCount -gt 1) {
    Add-Check 'WARN' "检测到 $simCount 个 opensim-sim 进程并存" '上次异常退出残留的孤儿进程会导致仿真瞬移/卡死，请运行 stop.ps1 清理后再启动'
}

# ======================================================================
# 06 崩溃痕迹
# ======================================================================
Write-Host '[6/8] 收集崩溃痕迹...'
$f = Join-Path $DIR '06-crash-evidence.txt'

# 6.1 Windows 应用程序错误事件（近 7 天）
Capture $f 'Windows 事件日志：应用程序错误（近 7 天，Simulation 相关）' {
    Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1000, 1001; StartTime = (Get-Date).AddDays(-7) } -ErrorAction Stop |
        Where-Object { $_.Message -match 'opensim|redis-server|node\.exe|python' } |
        Select-Object -First 20 |
        ForEach-Object { "[{0:yyyy-MM-dd HH:mm:ss}] EventID {1}`n{2}`n---" -f $_.TimeCreated, $_.Id, $_.Message }
}

# 6.2 引擎 stderr 日志尺寸分析（0 字节 = 死在 main() 之前）
W $f "`n=== 引擎 stderr 日志分析（competition\scenarios\*\output\sim.stderr.log）==="
$simLogs = Get-ChildItem (Join-Path $PACK_ROOT 'competition\scenarios\*\output\sim.stderr.log') -ErrorAction SilentlyContinue
if ($simLogs) {
    foreach ($sl in $simLogs) {
        $scenario = $sl.Directory.Parent.Name
        W $f ("  {0,-20} {1,12:N0} bytes  最后修改 {2:yyyy-MM-dd HH:mm:ss}" -f $scenario, $sl.Length, $sl.LastWriteTime)
    }
    $newest = $simLogs | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest.Length -eq 0) {
        Add-Check 'FAIL' "最新 sim.stderr.log 为 0 字节（$($newest.Directory.Parent.Name)）" '引擎在 main() 之前就崩溃：多为缺 MinGW DLL / VC++ Redistributable / 被杀毒软件拦截。见 04-runtime-deps.txt 与 06 中事件日志'
    }
} else {
    W $f '  (未找到任何 sim.stderr.log —— 用户可能从未成功启动过仿真)'
}

# 6.3 controller stderr 尺寸
W $f "`n=== controller stderr 日志（competition\scenarios\*\output\controller.stderr.log）==="
$ctlLogs = Get-ChildItem (Join-Path $PACK_ROOT 'competition\scenarios\*\output\controller.stderr.log') -ErrorAction SilentlyContinue
if ($ctlLogs) {
    foreach ($cl in $ctlLogs) {
        W $f ("  {0,-20} {1,12:N0} bytes  最后修改 {2:yyyy-MM-dd HH:mm:ss}" -f $cl.Directory.Parent.Name, $cl.Length, $cl.LastWriteTime)
    }
} else {
    W $f '  (未找到)'
}

# ======================================================================
# 07 收集日志文件
# ======================================================================
Write-Host '[7/8] 打包日志...'
$logDest = Join-Path $DIR 'logs'
$copied = 0
foreach ($lf in (Get-ChildItem (Join-Path $PACK_ROOT 'run\logs\*') -File -ErrorAction SilentlyContinue)) {
    if (Copy-LogFile $lf.FullName $logDest) { $copied++ }
}
# 引擎/competition 输出（两个可能位置都收）
$simDest = Join-Path $DIR 'sim-output'
foreach ($of in (Get-ChildItem (Join-Path $PACK_ROOT 'run\sim-output\*') -File -ErrorAction SilentlyContinue)) {
    if (Copy-LogFile $of.FullName $simDest) { $copied++ }
}
foreach ($scenarioDir in (Get-ChildItem (Join-Path $PACK_ROOT 'competition\scenarios') -Directory -ErrorAction SilentlyContinue)) {
    $outDir = Join-Path $scenarioDir.FullName 'output'
    if (-not (Test-Path $outDir)) { continue }
    $prefix = "$($scenarioDir.Name)_"
    foreach ($name in @('sim.stderr.log', 'controller.stderr.log', 'controller.stdout.log', 'profile.log')) {
        $src = Join-Path $outDir $name
        if (Copy-LogFile $src $simDest $prefix) { $copied++ }
    }
    # prepared scenario + 最近 3 个评分结果
    foreach ($pf in (Get-ChildItem (Join-Path $outDir 'scenario_*_prepared.json') -ErrorAction SilentlyContinue)) {
        if (Copy-LogFile $pf.FullName $simDest $prefix) { $copied++ }
    }
    $evals = Get-ChildItem (Join-Path $outDir '*.evaluation.json') -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 3
    foreach ($ef in $evals) {
        if (Copy-LogFile $ef.FullName $simDest $prefix) { $copied++ }
    }
}
# scenario.json 本体也收（供运维核对 redis_port 是否被改写）
foreach ($sf in (Get-ChildItem (Join-Path $PACK_ROOT 'competition\scenarios\*\scenario.json') -ErrorAction SilentlyContinue)) {
    if (Copy-LogFile $sf.FullName $simDest "$($sf.Directory.Name)_") { $copied++ }
}
W (Join-Path $DIR 'logs\_README.txt') "共收集 $copied 个日志文件。超过 2MB 的文件只保留最后 2000 行。"
Add-Check 'PASS' "已收集 $copied 个日志/输出文件"

# ======================================================================
# 08 自动分析（症状 -> 可能原因）
# ======================================================================
Write-Host '[8/8] 自动分析日志...'
$hints = @()

function Search-Log {
    param([string]$Pattern, [string[]]$Files)
    foreach ($file in $files) {
        if (Test-Path $file) {
            $hit = Select-String -Path $file -Pattern $Pattern -SimpleMatch:$false -ErrorAction SilentlyContinue | Select-Object -First 3
            if ($hit) { return $true }
        }
    }
    return $false
}

$bridgeErr = Join-Path $PACK_ROOT 'run\logs\bridge.err'
$bridgeLog = Join-Path $PACK_ROOT 'run\logs\bridge.log'
$frontendErr = Join-Path $PACK_ROOT 'run\logs\frontend.err'
$redisLog = Join-Path $PACK_ROOT 'run\logs\redis.log'
$ctlErrFiles = @(Get-ChildItem (Join-Path $PACK_ROOT 'competition\scenarios\*\output\controller.stderr.log') -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })
$simErrFiles = @($simLogs | ForEach-Object { $_.FullName })

if (Search-Log 'EADDRINUSE' @($bridgeErr, $bridgeLog, $frontendErr)) {
    $hints += '日志含 EADDRINUSE：端口被占用导致服务启动失败。查看 05-ports-processes.txt 确认占用进程，或用 OPENSIM_*_PORT 环境变量换端口后重启 start.ps1'
}
if (Search-Log 'competition_crashed' @($bridgeLog)) {
    $hints += 'bridge 报告 competition_crashed：competition 进程崩溃。重点查看 sim-output\*_controller.stderr.log'
}
if (Search-Log 'JSONDecodeError' $ctlErrFiles) {
    $hints += 'controller 日志含 JSONDecodeError：scenario.json 可能带 BOM 或已损坏（PowerShell 写入的 JSON 常见坑）。见 04-runtime-deps.txt 的 BOM 检查'
}
if (Search-Log 'opensim-sim not found' $ctlErrFiles) {
    $hints += 'controller 日志含 "opensim-sim not found"：引擎二进制缺失，可能被杀毒软件删除。重新解压发布包并加白名单'
}
if (Search-Log 'ready_timeout|ready timeout' @($bridgeLog)) {
    $hints += 'bridge 报告 ready_timeout：controller 5 分钟内未就绪。查看 sim-output\*_sim.stderr.log 是否引擎启动卡住（常见于地形数据缺失导致全量扫描）'
}
if (Search-Log 'ModuleNotFoundError' $ctlErrFiles) {
    $hints += 'controller 日志含 ModuleNotFoundError：Python 依赖缺失或用户算法模块路径不对。见 04-runtime-deps.txt'
}
if (Search-Log 'Connection refused|ConnectionRefused' ($ctlErrFiles + $simErrFiles)) {
    $hints += '日志含连接拒绝：Redis 未就绪或端口不对。查看 logs\redis.log，并核对 scenario.json 的 redis_port 与 05 中实际端口'
}
if (Search-Log 'route pool|路线池' $simErrFiles) {
    $hints += '引擎日志含路线池为空：config\points.json / random_routes_20.json 缺失（见 03-package-integrity.txt），目标不导航会导致仿真秒结束'
}
foreach ($h in $hints) {
    Add-Check 'WARN' "自动分析: $h"
}

# ======================================================================
# 汇总
# ======================================================================
$summary = Join-Path $DIR '00-summary.txt'
W $summary '============================================================'
W $summary ' Simulation 诊断报告'
W $summary " 生成时间: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
W $summary " 包根目录: $PACK_ROOT"
W $summary '============================================================'
W $summary ''
$failCount = @($script:Results | Where-Object Status -eq 'FAIL').Count
$warnCount = @($script:Results | Where-Object Status -eq 'WARN').Count
$passCount = @($script:Results | Where-Object Status -eq 'PASS').Count
W $summary "检查汇总: $passCount 通过 / $warnCount 警告 / $failCount 失败"
W $summary ''
foreach ($r in $script:Results) {
    W $summary ("[{0,-4}] {1}" -f $r.Status, $r.Item)
    if ($r.Hint) { W $summary "       建议: $($r.Hint)" }
}
W $summary ''
W $summary '--- 文件清单 ---'
W $summary '01-system.txt            操作系统/硬件/权限'
W $summary '02-environment.txt       环境变量'
W $summary '03-package-integrity.txt 发布包文件完整性'
W $summary '04-runtime-deps.txt      VC++/Python/Redis 依赖、scenario.json BOM、.pyc 版本'
W $summary '05-ports-processes.txt   端口监听与进程状态'
W $summary '06-crash-evidence.txt    Windows 崩溃事件 + 引擎 stderr 分析'
W $summary 'logs/                    start.ps1 各组件日志（redis/bridge/frontend 等）'
W $summary 'sim-output/              引擎与 competition 的输出日志、场景配置、评分结果'

# 打 zip
if (-not $NoZip) {
    $zipPath = Join-Path $PACK_ROOT "simulation-diagnostics-$ts.zip"
    try {
        Compress-Archive -Path $DIR -DestinationPath $zipPath -Force -ErrorAction Stop
        Write-Host ''
        Write-Host '============================================================' -ForegroundColor Cyan
        Write-Host " 诊断完成: $passCount 通过 / $warnCount 警告 / $failCount 失败"
        Write-Host ''
        Write-Host ' 请将以下文件发回给运维人员：' -ForegroundColor Yellow
        Write-Host "   $zipPath" -ForegroundColor Green
        Write-Host '============================================================' -ForegroundColor Cyan
    } catch {
        Write-Host ''
        Write-Host "打 zip 失败: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "请直接把整个目录发给运维人员: $DIR" -ForegroundColor Yellow
    }
} else {
    Write-Host ''
    Write-Host "诊断完成（-NoZip）: $passCount 通过 / $warnCount 警告 / $failCount 失败"
    Write-Host "诊断目录: $DIR"
}
exit 0
