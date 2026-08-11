$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$SharedRoot = if ($env:TECHSARA_HOME) { $env:TECHSARA_HOME } else { Join-Path $HOME ".techsara" }
$BinDir = Join-Path $SharedRoot "bin"
$Uv = Join-Path $BinDir "uv.exe"
$UvVersion = "0.11.32"
$InstallerUrl = "https://github.com/astral-sh/uv/releases/download/0.11.32/uv-installer.ps1"
$InstallerSha256 = "d84b0d973693497f8c1c1d82b2d2f52e32e50c7c24efa3d925341bd6fc5238b2"
$Offline = $args -contains "--offline"

if (-not (Test-Path -LiteralPath $Uv -PathType Leaf)) {
    if ($Offline) {
        Write-Error "TechSara error: pinned uv is missing and --offline forbids downloading it."
        exit 2
    }
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
    $Installer = [IO.Path]::GetTempFileName()
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $InstallerUrl -OutFile $Installer
        $Actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $Installer).Hash.ToLowerInvariant()
        if ($Actual -ne $InstallerSha256) {
            throw "Pinned uv installer checksum mismatch."
        }
        $env:UV_UNMANAGED_INSTALL = $BinDir
        $env:UV_NO_MODIFY_PATH = "1"
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $Installer
        if ($LASTEXITCODE -ne 0) { throw "Verified uv installer failed." }
    }
    finally {
        Remove-Item -LiteralPath $Installer -Force -ErrorAction SilentlyContinue
    }
}

# `uv --version` prints "uv <version> (<target triple>)", so compare the
# version field rather than the whole line.
$VersionLine = (& $Uv --version 2>$null | Select-Object -First 1)
$ActualVersion = if ($VersionLine) { ($VersionLine -split '\s+')[1] } else { $null }
if ($ActualVersion -ne $UvVersion) {
    Write-Error "TechSara error: $Uv reports '$ActualVersion', not the pinned uv $UvVersion. Remove it and rerun to reinstall."
    exit 2
}

$env:TECHSARA_HOME = $SharedRoot
$env:UV_PYTHON_INSTALL_DIR = Join-Path $SharedRoot "runtimes/python"
$env:UV_CACHE_DIR = Join-Path $SharedRoot "downloads/uv-cache"
$env:PYTHONPATH = if ($env:PYTHONPATH) { "$(Join-Path $ProjectRoot 'launcher');$($env:PYTHONPATH)" } else { Join-Path $ProjectRoot "launcher" }
$UvArgs = @("run", "--no-project", "--managed-python", "--python", "3.12")
if ($Offline) { $UvArgs += "--offline" }
$UvArgs += @("--", "python", "-m", "techsara_cli")
$UvArgs += $args
& $Uv @UvArgs
exit $LASTEXITCODE
