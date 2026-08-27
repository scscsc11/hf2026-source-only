# diagnose-vcredist.ps1 - diagnostic script for VC++ 2015-2022 Redistributable dialog
# Purpose: identify which process / DLL chain triggers the dialog on user machines.
# Pure ASCII to avoid PowerShell 5.1 encoding pitfalls when copy-pasted across
# Git Bash / cmd / PowerShell ISE / Notepad.
#
# Usage (on the affected Windows machine, as Administrator):
#   1. unzip the release package to any directory
#   2. open PowerShell (Admin), cd to package root
#   3. run:  .\diagnose-vcredist.ps1
#   4. when prompted, in another PowerShell window run:  .\start.ps1
#   5. when the "VC++ 2015-2022" dialog appears, DO NOT click OK
#   6. come back here, press Enter to continue
#   7. close the dialog, press Enter again
#   8. send back:  run\logs\diag-<timestamp>.log
#
# Output: <PACK_ROOT>\run\logs\diag-<timestamp>.log

#Requires -Version 5.1
[CmdletBinding()]
param()

$ErrorActionPreference = 'Continue'
$PACK_ROOT = (Resolve-Path $PSScriptRoot).Path
$LOG_DIR   = Join-Path $PACK_ROOT 'run\logs'
if (-not (Test-Path $LOG_DIR)) { New-Item -ItemType Directory -Path $LOG_DIR -Force | Out-Null }
$STAMP = Get-Date -Format 'yyyyMMdd-HHmmss'
$LOG   = Join-Path $LOG_DIR ("diag-$STAMP.log")

function Write-Log {
    param([string]$Msg, [string]$Color = 'White')
    $line = "[{0:HH:mm:ss}] {1}" -f (Get-Date), $Msg
    Write-Host $line -ForegroundColor $Color
    # Use UTF-8 (no BOM) so Chinese / unicode chars survive in the log file.
    # Avoids the release-merge-regression BOM pitfall when other tools parse it.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::AppendAllText($LOG, $line + [Environment]::NewLine, $utf8NoBom)
}

# Try to set the console code page to UTF-8 so Chinese / accents render
# correctly on classic conhost.exe (Windows 10 1903+ supports chcp 65001).
try {
    $currentCp = (Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop).CodeSet
    if ($env:TERM -ne 'dumb' -and $Host.Name -eq 'ConsoleHost') {
        # chcp may fail inside some IDEs; ignore failure
        cmd /c 'chcp 65001 >nul 2>&1' | Out-Null
        [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
        $OutputEncoding = [System.Text.Encoding]::UTF8
    }
} catch {}

Write-Log "===== diagnosis start =====" Cyan
Write-Log "package root: $PACK_ROOT"
Write-Log "log file:    $LOG"

# --- [0] OS / arch / admin / PS ---
Write-Log ""
Write-Log "[0] Environment" Yellow
Write-Log ("  OS        : {0}" -f ([System.Environment]::OSVersion.VersionString))
Write-Log ("  Arch      : {0}" -f $env:PROCESSOR_ARCHITECTURE)
Write-Log ("  Is64BitOS : {0}" -f [Environment]::Is64BitOperatingSystem)
Write-Log ("  IsAdmin   : {0}" -f ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole('Administrator'))
Write-Log ("  PSVersion : {0}" -f $PSVersionTable.PSVersion)

# --- [0.5] Pending reboot + uptime + last vcredist install timing ---
Write-Log ""
Write-Log "[0.5] Reboot / install timing (IMPORTANT: vc_redist install often needs reboot)" Yellow
try {
    # (a) Windows-side pending reboot flag
    $pendingReboot = $false
    $reasons = @()
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') {
        $pendingReboot = $true; $reasons += 'WindowsUpdate-RebootRequired'
    }
    if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') {
        $pendingReboot = $true; $reasons += 'CBS-RebootPending'
    }
    $sessionManager = Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -ErrorAction SilentlyContinue
    if ($sessionManager.PendingFileRenameOperations) {
        $pendingReboot = $true; $reasons += 'PendingFileRenameOperations'
    }
    Write-Log ("  PendingReboot: {0}  Reasons: {1}" -f $pendingReboot, ($reasons -join ', '))
} catch {
    Write-Log "  (pending-reboot check failed: $_)" Red
}

# (b) System uptime
try {
    $os = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop
    $bootTime = $os.LastBootUpTime
    $uptime = (Get-Date) - $bootTime
    Write-Log ("  LastBootUpTime: {0:yyyy-MM-dd HH:mm:ss}" -f $bootTime)
    Write-Log ("  Uptime        : {0} days {1} hours {2} min" -f [int]$uptime.TotalDays, [int]$uptime.Hours, [int]$uptime.Minutes)
    if ($uptime.TotalDays -gt 30) {
        Write-Log "  >>> WARNING: system uptime > 30 days. Old boot may have stale SxS handles." Yellow
    }
} catch {
    Write-Log "  (uptime check failed: $_)" Red
}

# (c) Last vcredist install timestamp vs current boot
Write-Log ""
Write-Log "[0.6] vcredist install timestamps vs current boot" Yellow
try {
    $vcDirs = @(
        'C:\Windows\System32\vcruntime140.dll',
        'C:\Windows\System32\msvcp140.dll',
        'C:\Windows\System32\ucrtbase.dll'
    )
    foreach ($f in $vcDirs) {
        if (Test-Path $f) {
            $fi = Get-Item $f
            $installedAfterBoot = $fi.LastWriteTime -gt $bootTime
            $tag = if ($installedAfterBoot) { '<<AFTER BOOT (reboot needed to take effect)>>' } else { 'before boot' }
            Write-Log ("  {0,-32} LastWrite={1:yyyy-MM-dd HH:mm:ss}  {2}" -f $fi.Name, $fi.LastWriteTime, $tag)
            if ($installedAfterBoot) {
                Write-Log "  >>> CRITICAL: vcredist was installed AFTER current Windows boot." Red
                Write-Log "  >>>   Some SxS assemblies / file handles are only refreshed on reboot." Red
                Write-Log "  >>>   Try: shutdown /r /t 0   then re-run." Red
            }
        }
    }
} catch {
    Write-Log "  (install timestamp check failed: $_)" Red
}

# (d) wmic qfe list last 5 install times (for context)
Write-Log ""
Write-Log "[0.7] Most recent Windows updates (for context)" Yellow
try {
    $qfe = Get-HotFix -ErrorAction SilentlyContinue | Sort-Object InstalledOn -Descending | Select-Object -First 5
    foreach ($q in $qfe) {
        Write-Log ("  {0:yyyy-MM-dd}  {1}" -f $q.InstalledOn, $q.HotFixID)
    }
} catch {
    Write-Log "  (Get-HotFix unavailable: $_)" DarkGray
}

# --- [1] Installed VC++ runtimes (HKLM 64 + WOW64 32) ---
Write-Log ""
Write-Log "[1] Installed VC++ Redistributable (registry)" Yellow
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
foreach ($k in $vcKeys) {
    $v = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
    if ($v) {
        Write-Log ("  + INSTALLED {0}  Major={1} Bld={2} Installed={3}" -f $k, $v.Major, $v.Minor, $v.Installed) Green
    } else {
        Write-Log ("  - absent   {0}" -f $k) DarkGray
    }
}

# --- [1.5] Early verdict if reboot is the most likely cause ---
Write-Log ""
Write-Log "[1.5] Early verdict" Yellow
$needsReboot = $false
$reason = ''
# verdict criterion 1: any key DLL written after current boot
foreach ($f in @('C:\Windows\System32\vcruntime140.dll', 'C:\Windows\System32\msvcp140.dll')) {
    if (Test-Path $f) {
        $fi = Get-Item $f
        if ($fi.LastWriteTime -gt $bootTime) {
            $needsReboot = $true
            $reason = "$($fi.Name) was installed AFTER current Windows boot ($($fi.LastWriteTime) > $($bootTime))"
            break
        }
    }
}
# verdict criterion 2: PendingReboot flag set
if (-not $needsReboot -and $pendingReboot) {
    $needsReboot = $true
    $reason = "Windows reports a pending reboot: $($reasons -join ', ')"
}
# verdict criterion 3: no vcredist installed at all
if (-not $needsReboot) {
    $anyVcInstalled = $false
    foreach ($k in $vcKeys) {
        if (Get-ItemProperty -Path $k -ErrorAction SilentlyContinue) { $anyVcInstalled = $true; break }
    }
    if (-not $anyVcInstalled) {
        $needsReboot = $false  # genuine missing
        $reason = "no vcredist installed in registry"
    }
}
if ($needsReboot) {
    Write-Log "  >>> HIGH-PROBABILITY ROOT CAUSE: REBOOT NEEDED <<<" Red
    Write-Log ("      Reason: {0}" -f $reason) Red
    Write-Log ""
    Write-Log "  >>> ACTION: please run the following as Administrator, then re-launch:" Yellow
    Write-Log "          shutdown /r /t 0" Yellow
    Write-Log "      After reboot, run start.ps1 again. If dialog persists, run this" Yellow
    Write-Log "      diagnostic again and send back the log." Yellow
    Write-Log ""
    Write-Log "  (Continuing deeper diagnosis anyway, in case reboot does not help...)" DarkGray
}

# --- [2] DLL existence matrix ---
Write-Log ""
Write-Log "[2] Key VC++/UCRT DLL presence matrix" Yellow
$dllNames = @(
    'vcruntime140.dll', 'vcruntime140_1.dll',
    'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
    'ucrtbase.dll', 'concrt140.dll', 'vccorlib140.dll'
)
$ucrtNames = @(
    'api-ms-win-crt-runtime-l1-1-0.dll',
    'api-ms-win-crt-stdio-l1-1-0.dll',
    'api-ms-win-crt-heap-l1-1-0.dll',
    'api-ms-win-crt-string-l1-1-0.dll',
    'api-ms-win-crt-math-l1-1-0.dll',
    'api-ms-win-crt-locale-l1-1-0.dll',
    'api-ms-win-crt-environment-l1-1-0.dll',
    'api-ms-win-crt-filesystem-l1-1-0.dll',
    'api-ms-win-crt-convert-l1-1-0.dll',
    'api-ms-win-crt-time-l1-1-0.dll',
    'api-ms-win-crt-process-l1-1-0.dll',
    'api-ms-win-crt-conio-l1-1-0.dll'
)
$matrix = [ordered]@{}
foreach ($n in $dllNames + $ucrtNames) { $matrix[$n] = @{ Sys = $false; Wow = $false; Py = $false } }
if (Test-Path 'C:\Windows\System32') {
    Get-ChildItem 'C:\Windows\System32' -Filter '*.dll' -ErrorAction SilentlyContinue | ForEach-Object {
        if ($matrix.Contains($_.Name)) { $matrix[$_.Name].Sys = $true }
    }
}
if (Test-Path 'C:\Windows\SysWOW64') {
    Get-ChildItem 'C:\Windows\SysWOW64' -Filter '*.dll' -ErrorAction SilentlyContinue | ForEach-Object {
        if ($matrix.Contains($_.Name)) { $matrix[$_.Name].Wow = $true }
    }
}
$pyDir = Join-Path $PACK_ROOT 'python'
if (Test-Path $pyDir) {
    Get-ChildItem $pyDir -Filter '*.dll' -ErrorAction SilentlyContinue | ForEach-Object {
        if ($matrix.Contains($_.Name)) { $matrix[$_.Name].Py = $true }
    }
}
Write-Log ("  {0,-42} {1,-6} {2,-6} {3,-6}" -f 'DLL', 'Sys32', 'WOW64', 'Pack')
foreach ($k in $matrix.Keys) {
    $s = if ($matrix[$k].Sys) {'YES'} else {'-'}
    $w = if ($matrix[$k].Wow) {'YES'} else {'-'}
    $p = if ($matrix[$k].Py)  {'YES'} else {'-'}
    Write-Log ("  {0,-42} {1,-6} {2,-6} {3,-6}" -f $k, $s, $w, $p)
}

# --- [3] PATH ---
Write-Log ""
Write-Log "[3] Current PATH" Yellow
$pathStr = $env:PATH
$pathDisplay = ($pathStr -split ';') -join "`n  "
Write-Log ("  {0}" -f $pathDisplay)

# --- [4] Baseline processes (before starting start.ps1) ---
Write-Log ""
Write-Log "[4] Baseline processes (opensim/redis/node/python family)" Yellow
$before = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match 'opensim|redis|node|python|sim|render' -or
    ($_.Path -and $_.Path -like "*$PACK_ROOT*")
} | Select-Object Id, Name, Path
foreach ($p in $before) {
    Write-Log ("  PID={0,6} Name={1,-25} Path={2}" -f $p.Id, $p.Name, $p.Path)
}

# --- [5] Wait for dialog ---
Write-Log ""
Write-Log "[5] Now reproduce the dialog" Yellow
Write-Log ""
Write-Log "  In a SECOND Admin PowerShell window, do EXACTLY this:" Cyan
Write-Log ""
Write-Log "    cd '$PACK_ROOT'" Cyan
Write-Log "    .\start.ps1" Cyan
Write-Log ""
Write-Log "  Then in the browser frontend that opens:" Cyan
Write-Log ""
Write-Log "    1. Wait for UI to fully load (map + entity list + weather dropdown)" Cyan
Write-Log "    2. Pick any scenario from the right-side list" Cyan
Write-Log "    3. Click the bottom-right 'Begin / Start Simulation' button" Cyan
Write-Log "    4. The VC++ 2015-2022 dialog should appear over the UI" Cyan
Write-Log ""
Write-Log "  When the dialog is on screen:" Cyan
Write-Log ""
Write-Log "    *** DO NOT click OK / Confirm ***" Red
Write-Log "    *** LEAVE THE DIALOG ON SCREEN ***" Red
Write-Log ""
Write-Log "  Then come back to THIS window and press Enter." Cyan
Write-Log ""
Write-Log "  (If no dialog appears, just press Enter anyway and the script" DarkGray
Write-Log "   will keep going so we still see the process state.)" DarkGray
Read-Host "Press Enter when dialog is on screen (or immediately if no dialog)"

# --- [6] Snapshot current suspicious processes ---
Write-Log ""
Write-Log "[6] Suspicious processes snapshot" Yellow
$procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match 'opensim|redis|node|python|sim|render|msi|vc_redist|setup|install|host|werfault'
}
foreach ($p in $procs) {
    Write-Log ("  PID={0,6} Name={1,-30} StartTime={2:HH:mm:ss} Title={3}" -f $p.Id, $p.Name, $p.StartTime, $p.MainWindowTitle)
}

# --- [7] Process tree ---
Write-Log ""
Write-Log "[7] Process tree (opensim/redis/node/python/cmd/powershell)" Yellow
try {
    $tree = Get-CimInstance -ClassName Win32_Process -ErrorAction Stop |
        Where-Object { $_.Name -match 'opensim|redis|node|python|cmd|powershell|sim|render' } |
        Select-Object ProcessId, ParentProcessId, Name, CommandLine
    foreach ($p in $tree) {
        $cl = if ($p.CommandLine.Length -gt 200) { $p.CommandLine.Substring(0, 200) + '...' } else { $p.CommandLine }
        Write-Log ("  PID={0,6} PPID={1,6} Name={2,-22} Cmd={3}" -f $p.ProcessId, $p.ParentProcessId, $p.Name, $cl)
    }
} catch {
    Write-Log "  (Get-CimInstance Win32_Process failed: $_)" Red
}

# --- [8] Signed/un-signed status of opensim-rooted binaries ---
Write-Log ""
Write-Log "[8] Signature status of package-rooted binaries" Yellow
$allProcs = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path -like "*$PACK_ROOT*" }
foreach ($p in $allProcs) {
    try {
        $sig = Get-AuthenticodeSignature -FilePath $p.Path -ErrorAction SilentlyContinue
        Write-Log ("  PID={0,6} Name={1,-25} SignStatus={2,-15} Path={3}" -f $p.Id, $p.Name, $sig.Status, $p.Path)
    } catch {
        Write-Log ("  PID={0,6} Name={1,-25} (no signature) Path={2}" -f $p.Id, $p.Name, $p.Path)
    }
}

# --- [9] Capture loaded DLLs per process ---
Write-Log ""
Write-Log "[9] Press Enter after closing the dialog to capture loaded DLLs" Yellow
Read-Host "Press Enter (dialog now closed)"

Write-Log ""
Write-Log "[10] Loaded DLL inventory (filtering VC/UCRT-related)" Yellow
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class M {
    [DllImport("kernel32.dll")] public static extern IntPtr OpenProcess(uint a, bool b, int c);
    [DllImport("psapi.dll")]   public static extern bool EnumProcessModules(IntPtr h, IntPtr[] m, uint s, out uint n);
    [DllImport("psapi.dll")]   public static extern uint GetModuleFileNameEx(IntPtr h, IntPtr m, System.Text.StringBuilder s, uint n);
}
"@
    $procs2 = Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.Id -and $_.Path -and $_.Path -like "*$PACK_ROOT*" }
    foreach ($p in $procs2) {
        $h = [M]::OpenProcess(0x0410, $false, $p.Id)
        if ($h -eq [IntPtr]::Zero) { continue }
        $mods = New-Object IntPtr[] 1024
        $out = 0
        if ([M]::EnumProcessModules($h, $mods, 1024 * [IntPtr]::Size, [ref]$out)) {
            $count = [int]($out / [IntPtr]::Size)
            Write-Log ("  --- PID={0} ({1}) loaded {2} modules ---" -f $p.Id, $p.Name, $count)
            for ($i = 0; $i -lt $count; $i++) {
                $sb = New-Object System.Text.StringBuilder 512
                $len = [M]::GetModuleFileNameEx($h, $mods[$i], $sb, 512)
                if ($len -gt 0) {
                    $path = $sb.ToString()
                    if ($path -match 'vcruntime|msvcp|ucrtbase|api-ms-win-crt|vc_redist') {
                        Write-Log ("    [VC++] {0}" -f $path) Cyan
                    }
                }
            }
        }
    }
} catch {
    Write-Log "  (EnumProcessModules failed: $_)" Red
}

# --- [11] WMI filter for recent processes (CIM datetime format) ---
Write-Log ""
Write-Log "[11] Processes started in the last 10 minutes" Yellow
try {
    $cutoff = (Get-Date).AddMinutes(-10)
    $cimCutoff = [Management.ManagementDateTimeConverter]::ToDmtfDateTime($cutoff)
    $recent = Get-CimInstance -ClassName Win32_Process -Filter "CreationDate >= '$cimCutoff'" -ErrorAction Stop |
        Where-Object { $_.Name -notmatch '^(System|svchost|explorer|dwm)$' } |
        Select-Object ProcessId, ParentProcessId, Name, CommandLine, CreationDate |
        Sort-Object CreationDate -Descending | Select-Object -First 50
    foreach ($p in $recent) {
        $cl = if ($p.CommandLine.Length -gt 200) { $p.CommandLine.Substring(0, 200) + '...' } else { $p.CommandLine }
        Write-Log ("  PID={0,6} PPID={1,6} Name={2,-22} Created={3:HH:mm:ss} Cmd={4}" -f $p.ProcessId, $p.ParentProcessId, $p.Name, $p.CreationDate, $cl)
    }
} catch {
    Write-Log "  (recent process query failed: $_)" Red
}

Write-Log ""
Write-Log "===== diagnosis done. log written to: $LOG =====" Cyan
Write-Log "Please send run\logs\$((Split-Path $LOG -Leaf)) to the developer." Yellow
Write-Host ""
Write-Host "Press any key to exit..." -ForegroundColor Green
[void]$Host.UI.RawUI.ReadKey('NoEcho,IncludeKeyDown')