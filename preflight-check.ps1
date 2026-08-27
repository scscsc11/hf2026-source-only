# preflight-check.ps1 - OpenSim release package environment pre-flight checklist
# Runs locally before opening any network ports; prints a report with PASS/FAIL/WARN.
# Usage: cd <package root>  ;  .\preflight-check.ps1
#
# Output: run\logs\preflight-<timestamp>.log

#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$PACK_ROOT = (Resolve-Path $PSScriptRoot).Path
$LOG_DIR   = Join-Path $PACK_ROOT 'run\logs'
$STAMP     = Get-Date -Format 'yyyyMMdd-HHmmss'
$LOG       = Join-Path $LOG_DIR ("preflight-$STAMP.log")

if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null }

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-Log {
    param([string]$Msg, [string]$Color = 'White')
    $line = "[{0:HH:mm:ss}] {1}" -f (Get-Date), $Msg
    Write-Host $line -ForegroundColor $Color
    [System.IO.File]::AppendAllText($LOG, $line + [Environment]::NewLine, $utf8NoBom)
}

$pass = 0
$warn = 0
$fail = 0

function Result {
    param([string]$Name, [string]$Status, [string]$Detail, [string]$Color)
    Write-Log ("  [{0,-4}] {1,-52} {2}" -f $Status, $Name, $Detail) $Color
    switch ($Status) {
        'PASS' { $script:pass++ }
        'WARN' { $script:warn++ }
        'FAIL' { $script:fail++ }
    }
}

Write-Log "===== OpenSim preflight check start =====" Cyan
Write-Log "package root: $PACK_ROOT"
Write-Log "log file:    $LOG"
Write-Log ""

# [1] Core binaries exist
Write-Log "[1] Core binaries" Yellow
foreach ($name in @('opensim-sim.exe', 'opensim-render-ctl.exe', 'bin\node.exe', 'bin\redis-server.exe')) {
    $p = Join-Path $PACK_ROOT $name
    if (Test-Path $p) { Result "$name exists" 'PASS' $p Green }
    else { Result "$name exists" 'FAIL' 'missing' Red }
}

# [2] MinGW runtime DLLs for engine
Write-Log ""
Write-Log "[2] MinGW runtime DLLs (engine load-time dependencies)" Yellow
$mingwDlls = @('libwinpthread-1.dll', 'libgcc_s_seh-1.dll', 'libstdc++-6.dll')
foreach ($dll in $mingwDlls) {
    $p = Join-Path $PACK_ROOT $dll
    if (Test-Path $p) { Result "$dll in package root" 'PASS' $p Green }
    else {
        # if it's not in package root but in PATH, it might still work on this machine
        $inPath = $null -ne (Get-Command $dll -ErrorAction SilentlyContinue)
        if ($inPath) { Result "$dll in package root" 'WARN' 'not in package but found in PATH' Yellow }
        else { Result "$dll in package root" 'FAIL' 'missing; opensim-sim will fail with 0xC0000135' Red }
    }
}

# [3] Engine DLL dependencies (using objdump if available, else strings heuristic)
Write-Log ""
Write-Log "[3] Engine DLL import check" Yellow
$engine = Join-Path $PACK_ROOT 'opensim-sim.exe'
$requiredDlls = @('libwinpthread-1.dll')
if (Test-Path $engine) {
    $objdump = Get-Command objdump -ErrorAction SilentlyContinue
    if ($objdump) {
        $imports = & $objdump.Source -p $engine 2>$null | Select-String 'DLL Name:' | ForEach-Object { ($_ -split 'DLL Name: ')[1].Trim() }
        Write-Log "  objdump imports: $($imports -join ', ')" DarkGray
        foreach ($dll in $requiredDlls) {
            if ($imports -contains $dll) {
                $p = Join-Path $PACK_ROOT $dll
                if (Test-Path $p) { Result "import $dll satisfied" 'PASS' 'bundled' Green }
                else { Result "import $dll satisfied" 'FAIL' 'imported but not bundled' Red }
            }
        }
    } else {
        Write-Log "  objdump not found; using known required list" DarkGray
        foreach ($dll in $requiredDlls) {
            $p = Join-Path $PACK_ROOT $dll
            if (Test-Path $p) { Result "import $dll satisfied" 'PASS' 'bundled' Green }
            else { Result "import $dll satisfied" 'FAIL' 'not bundled' Red }
        }
    }
}

# [4] Bundled Python + vcredist DLLs
Write-Log ""
Write-Log "[4] Bundled Python + VC++ runtime" Yellow

$vcInstaller = Join-Path $PACK_ROOT 'vc_redist.x64.exe'
if (Test-Path $vcInstaller) { Result 'vc_redist.x64.exe bundled' 'PASS' 'present' Green }
else { Result 'vc_redist.x64.exe bundled' 'WARN' 'not bundled; setup.ps1 cannot auto-install' Yellow }

$vcKeys = @(
    'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64',
    'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.1\VC\Runtimes\x64',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.1\VC\Runtimes\x64',
    'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.2\VC\Runtimes\x64',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.2\VC\Runtimes\x64',
    'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.3\VC\Runtimes\x64',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.3\VC\Runtimes\x64',
    'HKLM:\SOFTWARE\Microsoft\VisualStudio\14.4\VC\Runtimes\x64',
    'HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.4\VC\Runtimes\x64'
)
$vcInstalled = $false
$vcMaxBld = 0
foreach ($k in $vcKeys) {
    $v = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
    if ($v -and $v.Installed -and $v.Bld) {
        $vcInstalled = $true
        if ($v.Bld -gt $vcMaxBld) { $vcMaxBld = $v.Bld }
    }
}
if ($vcInstalled) { Result 'VC++ Redistributable installed' 'PASS' "build $vcMaxBld" Green }
else { Result 'VC++ Redistributable installed' 'WARN' 'not installed; run setup.ps1 first' Yellow }

$py = Join-Path $PACK_ROOT 'python\python.exe'
if (Test-Path $py) {
    try {
        $ver = & $py --version 2>&1
        Result 'python.exe runs' 'PASS' $ver Green
    } catch {
        Result 'python.exe runs' 'FAIL' 'python.exe failed to launch' Red
    }
    foreach ($dll in @('python312.dll', 'python3.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')) {
        $p = Join-Path $PACK_ROOT "python\$dll"
        if (Test-Path $p) { Result "python\$dll" 'PASS' 'present' Green }
        else { Result "python\$dll" 'FAIL' 'missing' Red }
    }
} else {
    Result 'python.exe in package' 'FAIL' 'missing' Red
}

# [5] Python packages required by runner
Write-Log ""
Write-Log "[5] Python package availability (redis, yaml)" Yellow
if (Test-Path $py) {
    $redisOk = & $py -c "import redis" 2>&1
    if ($LASTEXITCODE -eq 0) { Result 'import redis' 'PASS' 'ok' Green }
    else { Result 'import redis' 'FAIL' 'redis module not available' Red }
    $yamlOk = & $py -c "import yaml" 2>&1
    if ($LASTEXITCODE -eq 0) { Result 'import yaml' 'PASS' 'ok' Green }
    else { Result 'import yaml' 'FAIL' 'yaml module not available' Red }
} else {
    Write-Log "  skipped (python.exe missing)" DarkGray
}

# [6] Required config / data files
Write-Log ""
Write-Log "[6] Required config / data files" Yellow
$requiredFiles = @(
    'config\defaults.json',
    'config\HeightSample.csv',
    'config\GridDataAll_18.csv',
    'config\models\uav.json',
    'competition\sdk\__init__.py',
    'competition\sdk\core\runner.py',
    'competition\sdk\_vendored\sim_runner.py',
    'visualization\dist-bridge\bridge\index.js',
    'frontend\index.html'
)
foreach ($f in $requiredFiles) {
    $p = Join-Path $PACK_ROOT $f
    if (Test-Path $p) { Result "file $f" 'PASS' 'present' Green }
    else { Result "file $f" 'FAIL' 'missing' Red }
}

# [6.5] Python bytecode version check (stale cpython-3xx.pyc from a different interpreter)
Write-Log ""
Write-Log "[6.5] Python bytecode version consistency" Yellow
$bundledPyVersion = $null
$pyExe = Join-Path $PACK_ROOT 'python\python.exe'
if (Test-Path $pyExe) {
    $verLine = & $pyExe -c "import sys; print(sys.version_info.major, sys.version_info.minor)" 2>&1
    if ($LASTEXITCODE -eq 0) {
        $parts = $verLine -split '\s+'
        $bundledPyVersion = "$($parts[0]).$($parts[1])"
    }
}
$versionTag = ($bundledPyVersion -replace '\.', '')
$stalePyc = Get-ChildItem -Path (Join-Path $PACK_ROOT 'competition') -Recurse -Filter '*.pyc' -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notmatch "\.cpython-$versionTag\." -and $_.Name -match 'cpython-\d+\.' } |
    Select-Object -First 5
if ($stalePyc) {
    Result 'stale .pyc magic number' 'WARN' "found $($stalePyc.Count) .pyc not matching bundled Python $bundledPyVersion; fallback to source" Yellow
    $stalePyc | ForEach-Object { Write-Log "    $($_.FullName.Replace($PACK_ROOT, '').TrimStart('\'))" DarkGray }
} else {
    Result 'stale .pyc magic number' 'PASS' "all .pyc match bundled Python $bundledPyVersion or no .pyc found" Green
}

# [7] Scenario JSON encoding (BOM check)
Write-Log ""
Write-Log "[7] Scenario JSON encoding (BOM)" Yellow
$scenarioDir = Join-Path $PACK_ROOT 'competition\scenarios'
if (Test-Path $scenarioDir) {
    $scenarios = Get-ChildItem $scenarioDir -Filter 'scenario.json' -Recurse -ErrorAction SilentlyContinue | Select-Object -First 5
    foreach ($s in $scenarios) {
        $bytes = [IO.File]::ReadAllBytes($s.FullName) | Select-Object -First 3
        if ($bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            Result "$($s.FullName.Replace($PACK_ROOT, '').TrimStart('\'))" 'FAIL' 'has UTF-8 BOM; Python json.loads will fail' Red
        } else {
            Result "$($s.FullName.Replace($PACK_ROOT, '').TrimStart('\'))" 'PASS' 'no BOM' Green
        }
    }
}

# [8] Port availability
Write-Log ""
Write-Log "[8] Default ports availability" Yellow
function Test-Port {
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop
        return $true
    } catch { return $false }
}
foreach ($port in @(6379, 8080, 8081, 3000)) {
    if (Test-Port $port) { Result "port $port in use" 'WARN' "another process is listening" Yellow }
    else { Result "port $port in use" 'PASS' 'free' Green }
}

# [9] PowerShell execution policy (current user scope)
Write-Log ""
Write-Log "[9] PowerShell execution policy" Yellow
$policy = Get-ExecutionPolicy -Scope CurrentUser -ErrorAction SilentlyContinue
if ($policy -in @('RemoteSigned', 'Unrestricted', 'Bypass')) { Result "CurrentUser policy" 'PASS' $policy Green }
else { Result "CurrentUser policy" 'WARN' "$policy - may block scripts on some machines" Yellow }

# [10] Admin check (for service binding below 1024, not needed here)
Write-Log ""
Write-Log "[10] Permissions / admin" Yellow
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator')
if ($admin) { Result 'running as admin' 'PASS' 'yes' Green }
else { Result 'running as admin' 'WARN' 'non-admin; usually fine, but may affect firewall prompts' Yellow }

# [11] Path length sanity
Write-Log ""
Write-Log "[11] Path length" Yellow
$len = $PACK_ROOT.Length
if ($len -gt 200) { Result 'package root length' 'WARN' "$len chars; close to MAX_PATH 260 limit" Yellow }
elseif ($len -gt 120) { Result 'package root length' 'WARN' "$len chars; prefer shorter path" Yellow }
else { Result 'package root length' 'PASS' "$len chars" Green }

# [12] Redis quick start test
Write-Log ""
Write-Log "[12] Redis binary smoke test" Yellow
$redis = Join-Path $PACK_ROOT 'bin\redis-server.exe'
if (Test-Path $redis) {
    try {
        # Use --version as a lightweight binary sanity check instead of starting a server,
        # which avoids port/permissions/cleanup complexity in a pre-flight script.
        $output = & $redis --version 2>&1
        if ($LASTEXITCODE -eq 0 -and $output -match 'Redis') {
            Result 'redis-server --version' 'PASS' ($output -split "`n" | Select-Object -First 1) Green
        } else {
            Result 'redis-server --version' 'FAIL' "exit=$LASTEXITCODE output=$output" Red
        }
    } catch {
        Result 'redis-server --version' 'FAIL' "$_" Red
    }
} else {
    Result 'redis-server --version' 'FAIL' 'missing' Red
}

# [13] Windows version / UCRT availability
Write-Log ""
Write-Log "[13] Windows compatibility" Yellow
$os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction SilentlyContinue
$build = [System.Environment]::OSVersion.Version.Build
if ($build -ge 10240) { Result "Windows build $build" 'PASS' 'Windows 10+ (UCRT available)' Green }
else { Result "Windows build $build" 'FAIL' 'Windows 10+ required for bundled Python' Red }

# [14] Write permission to run/ output dirs
Write-Log ""
Write-Log "[14] Write permission to run directories" Yellow
$testDirs = @('run\logs', 'run\pids', 'run\redis')
foreach ($d in $testDirs) {
    $p = Join-Path $PACK_ROOT $d
    try {
        if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
        $testFile = Join-Path $p "preflight-write-test-$STAMP.txt"
        [IO.File]::WriteAllText($testFile, 'test', $utf8NoBom)
        Remove-Item $testFile -Force
        Result "write $d" 'PASS' 'ok' Green
    } catch {
        Result "write $d" 'FAIL' "$_" Red
    }
}

Write-Log ""
Write-Log "===== Summary: PASS=$pass  WARN=$warn  FAIL=$fail =====" Cyan
if ($fail -gt 0) { Write-Log "Please fix all FAIL items before distributing or running this package." Red }
elseif ($warn -gt 0) { Write-Log "Package will likely run; review WARN items for edge cases." Yellow }
else { Write-Log "All checks passed." Green }
Write-Log "log written to: $LOG"

# Return exit code proportional to severity
if ($fail -gt 0) { exit 1 } else { exit 0 }