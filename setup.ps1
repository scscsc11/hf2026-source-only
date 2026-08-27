# setup.ps1 — Windows 版环境检测与依赖安装
# 检测 python / pip / redis(python) / pyyaml 等依赖，缺失则自动 pip 安装。
# 幂等：已装的跳过，重复运行安全。

#Requires -Version 5.1
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ── 工具函数 ──
function Test-Command {
    param([string]$Name)
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-VcRedistInstalledVersion {
    # Checks registry for any MSVC 14.x (2015-2022) redistributable.
    # Returns the Major.Minor.Build string, or $null if not found.
    $keys = @(
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
    $latestBld = 0
    foreach ($k in $keys) {
        $v = Get-ItemProperty -Path $k -ErrorAction SilentlyContinue
        if ($v -and $v.Installed -and $v.Bld) {
            if ($v.Bld -gt $latestBld) { $latestBld = $v.Bld }
        }
    }
    if ($latestBld -gt 0) { return $latestBld }
    return $null
}

function Install-VcRedist {
    param([string]$InstallerPath)
    # Run Microsoft's installer silently. May require admin rights.
    # 0   = success
    # 1638 = already installed / newer version present
    # 3010 = success, reboot required
    $proc = Start-Process -FilePath $InstallerPath -ArgumentList '/install', '/passive', '/norestart' -Wait -PassThru
    return $proc.ExitCode
}

function Get-PythonExe {
    # 优先使用发行包内捆绑的 Python,实现不依赖目标机系统 Python。
    # 捆绑 Python 位于 <包根>\python\python.exe(由 package-release.ps1 准备)。
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
    if (Test-Command 'python') { return 'python' }
    if (Test-Command 'py')     { return 'py' }
    return $null
}

function Invoke-Python {
    param([string]$Script, [string]$PythonExe)
    # 用 try/catch 包裹并把 stderr → stdout，避免 Set-StrictMode 下
    # python -c 失败时抛 NativeCommandError 中断整个 setup 流程。
    try {
        $output = & $PythonExe -c $Script 2>&1
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

# ── 0. Microsoft Visual C++ 2015-2022 Redistributable (x64) ──
# The bundled Python 3.12 is built with MSVC and needs VCRUNTIME140 + UCRT.
# This step detects the redist; if absent, it runs the bundled installer.
# The installer may require administrator rights on some machines.
$packRoot = $PSScriptRoot
$vcInstaller = Join-Path $packRoot 'vc_redist.x64.exe'
$vcBld = Get-VcRedistInstalledVersion
if ($vcBld) {
    Write-Host "✓ VC++ Redistributable detected (build $vcBld)"
} else {
    Write-Host '⚠️  VC++ Redistributable not detected. This is required by the bundled Python.' -ForegroundColor Yellow
    if (Test-Path $vcInstaller) {
        Write-Host '  Installing bundled vc_redist.x64.exe silently...' -ForegroundColor Yellow
        $exitCode = Install-VcRedist -InstallerPath $vcInstaller
        switch ($exitCode) {
            0       { Write-Host '✓ VC++ Redistributable installed successfully.' Green }
            1638    { Write-Host '✓ VC++ Redistributable already present (or newer version installed).' Green }
            3010    { Write-Host '✓ VC++ Redistributable installed. A system reboot may be required.' Yellow }
            default {
                Write-Host "✗ VC++ Redistributable installer returned exit code $exitCode" -ForegroundColor Red
                Write-Host '  Please install it manually: https://aka.ms/vs/17/release/vc_redist.x64.exe' -ForegroundColor Red
                exit 1
            }
        }
    } else {
        Write-Host '✗ Bundled vc_redist.x64.exe not found. The bundled Python may fail to start.' -ForegroundColor Red
        Write-Host '  Please install it manually: https://aka.ms/vs/17/release/vc_redist.x64.exe' -ForegroundColor Red
        exit 1
    }
}

# ── 1. Python 解释器 ──
$python = Get-PythonExe
if (-not $python) {
    Write-Host '✗ 未找到 python。请从 https://www.python.org/downloads/ 安装 Python 3.10+，'
    Write-Host '  或用 winget install Python.Python.3.12 安装。'
    exit 1
}
Write-Host "✓ Python: $python"

# ── 2. pip ──
& $python -m pip --version 2>$null | Out-Null
$pipOk = $LASTEXITCODE -eq 0
if (-not $pipOk) {
    Write-Host '✗ pip 不可用，请重新安装 Python 时勾选 "Install pip"'
    exit 1
}
Write-Host '✓ pip 可用'

# ── 3. Python 包 redis / pyyaml ──
$needPip = @()
$redisOk = Invoke-Python -Script 'import redis' -PythonExe $python
if (-not $redisOk) { $needPip += 'redis' }
$yamlOk = Invoke-Python -Script 'import yaml' -PythonExe $python
if (-not $yamlOk) { $needPip += 'pyyaml' }

if ($needPip) {
    Write-Host "缺少 Python 包: $($needPip -join ', ')"
    & $python -m pip install --user @needPip
    if ($LASTEXITCODE -ne 0) {
        Write-Host '✗ pip install 失败，请手动运行: python -m pip install --user redis pyyaml'
        exit 1
    }
    Write-Host "✓ 已安装: $($needPip -join ', ')"
} else {
    Write-Host '✓ Python 包 redis + pyyaml 就绪'
}

# ── 4. 系统工具（Win10+ 内置） ──
if (Test-Command 'curl') {
    Write-Host '✓ curl 可用'
} else {
    Write-Host '⚠️  curl 不可用（Win10 1809+ 内置，建议升级 Windows）'
}
if (Test-Command 'tar') {
    Write-Host '✓ tar 可用'
} else {
    Write-Host '⚠️  tar 不可用（Win10 1809+ 内置，建议升级 Windows）'
}

# ── 5. 引擎二进制存在性（打包后才有） ──
$packRoot = $PSScriptRoot
$simExe = Join-Path $packRoot 'opensim-sim.exe'
if (Test-Path $simExe) {
    Write-Host "✓ opensim-sim.exe 就位"
} else {
    Write-Host '⚠️  opensim-sim.exe 缺失（如在源码仓库运行属正常；release 包内缺失说明打包失败）'
}

Write-Host ''
Write-Host '✓ 环境就绪，可运行: .\start.ps1'
