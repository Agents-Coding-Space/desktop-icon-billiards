#Requires -Version 7.0
<#
.SYNOPSIS
    Package Desktop Icon Billiards as a Windows x64 single-file exe.

.DESCRIPTION
    The single source of truth for the version is __version__ in dib/__init__.py; this script reads it at build time,
    generates build/version_info.txt and passes it to PyInstaller, so neither the spec file nor the
    version resource hard-codes a version.

    Output: dist/DesktopIconBilliards-<version>-win-x64.exe (old files in dist are kept).

    On first run, uv creates .venv at the repo root and installs requirements-build.txt;
    Windows 10/11 x64 and uv (https://docs.astral.sh/uv/) are prerequisites; Python 3.12 managed by uv is used by default.

.PARAMETER SkipDeps
    Skip creating .venv and installing dependencies; package directly with the existing .venv (for offline or CI reproduced builds).

.EXAMPLE
    pwsh -File build.ps1

.EXAMPLE
    pwsh -File build.ps1 -SkipDeps
#>
[CmdletBinding()]
param(
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot

$repoRoot = $PSScriptRoot
$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
$venvDir = Join-Path $repoRoot '.venv'
$requirementsBuild = Join-Path $repoRoot 'requirements-build.txt'
$buildDir = Join-Path $repoRoot 'build'
$versionInfoFile = Join-Path $buildDir 'version_info.txt'
$distDir = Join-Path $repoRoot 'dist'
$workDir = Join-Path $buildDir 'pyinstaller'
$specFile = Join-Path $repoRoot 'DesktopIconBilliards.spec'

function Write-Step([string]$Message) {
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-Uv {
    $command = Get-Command uv -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $fallback = Join-Path $env:USERPROFILE '.local\bin\uv.exe'
    if (Test-Path -LiteralPath $fallback) {
        return $fallback
    }
    throw @'
uv not found; cannot create .venv automatically.
Install uv first: winget install --id=astral-sh.uv -e
Or see https://docs.astral.sh/uv/ and re-run build.ps1.
If you already have a working .venv, use: pwsh -File build.ps1 -SkipDeps
'@
}

if (-not $IsWindows) {
    throw 'Desktop Icon Billiards supports Windows x64 only; run build.ps1 on Windows 10/11.'
}

# ---------------------------------------------------------------------------
# 1. Virtual environment
# ---------------------------------------------------------------------------
Write-Step 'Checking Python virtual environment (.venv)'
if (-not (Test-Path -LiteralPath $venvDir)) {
    if ($SkipDeps) {
        throw ".venv not found and -SkipDeps was given. Drop -SkipDeps to let the script create it, or run manually: uv venv --managed-python --python 3.12 .venv"
    }
    $uv = Resolve-Uv
    Write-Step 'Creating virtual environment .venv'
    & $uv venv --managed-python --python 3.12 $venvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create .venv (uv exit code $LASTEXITCODE)."
    }
}
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw '.venv\Scripts\python.exe not found. Re-create a 64-bit Python virtual environment and retry.'
}

# ---------------------------------------------------------------------------
# 2. Dependencies
# ---------------------------------------------------------------------------
if ($SkipDeps) {
    Write-Step '-SkipDeps given; skipping dependency install, using existing .venv'
    & $venvPython -c 'import PyInstaller' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'PyInstaller is not installed in the current .venv; -SkipDeps cannot be used. Run build.ps1 once fully first.'
    }
} else {
    $uv = Resolve-Uv
    Write-Step 'Installing build dependencies (requirements-build.txt)'
    & $uv pip install --python $venvPython -r $requirementsBuild
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install build dependencies (uv exit code $LASTEXITCODE)."
    }
}

# ---------------------------------------------------------------------------
# 3. Version (single source: __version__ in dib/__init__.py)
# ---------------------------------------------------------------------------
Write-Step 'Reading version (source: __version__ in dib/__init__.py)'
$versionOutput = & $venvPython -c 'import dib; print(dib.__version__)'
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to read version: cannot import the dib package in the current .venv. Run from the repo root and make sure .venv has dependencies installed.'
}
$version = ([string]$versionOutput).Trim()
if ($version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Invalid version format: '$version'. Set __version__ in dib/__init__.py to major.minor.patch (e.g. 0.1.0)."
}
Write-Host "    Version: $version"

# ---------------------------------------------------------------------------
# 4. Version resource file
# ---------------------------------------------------------------------------
Write-Step 'Generating build/version_info.txt'
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
& $venvPython -X utf8 (Join-Path $repoRoot 'tools\gen_version_info.py') --version $version --output $versionInfoFile
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to generate version info (tools/gen_version_info.py exited non-zero).'
}
if (-not (Test-Path -LiteralPath $versionInfoFile)) {
    throw "Failed to generate version info: $versionInfoFile not found"
}

# ---------------------------------------------------------------------------
# 5. PyInstaller
# ---------------------------------------------------------------------------
$env:DIB_VERSION = $version
$env:DIB_VERSION_INFO_FILE = $versionInfoFile
New-Item -ItemType Directory -Force -Path $distDir | Out-Null

Write-Step 'Running PyInstaller (single file, windowed)'
Write-Host "    DIB_VERSION=$env:DIB_VERSION"
Write-Host "    DIB_VERSION_INFO_FILE=$env:DIB_VERSION_INFO_FILE"
& $venvPython -m PyInstaller --noconfirm --distpath $distDir --workpath $workDir $specFile
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller packaging failed (exit code $LASTEXITCODE)."
}

# ---------------------------------------------------------------------------
# 6. Verify output
# ---------------------------------------------------------------------------
$exePath = Join-Path $distDir "DesktopIconBilliards-$version-win-x64.exe"
if (-not (Test-Path -LiteralPath $exePath)) {
    throw "Packaging finished but the expected output was not found: $exePath"
}

# A workspace may inherit a Low integrity label. An EXE carrying that label
# starts at Low integrity and cannot unpack into the user's normal temp folder.
# Normalize only the release file; leave workspace and system ACLs untouched.
Write-Step 'Setting release EXE to normal user integrity level (Medium)'
& icacls.exe $exePath /setintegritylevel M
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to set the Medium integrity label on the release EXE. Run the build script from a normal-user PowerShell.'
}

$exe = Get-Item -LiteralPath $exePath
$hash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash
$sizeText = '{0:N1} MB' -f ($exe.Length / 1MB)

Write-Host ''
Write-Host 'Build complete' -ForegroundColor Green
Write-Host "  File:     $($exe.FullName)"
Write-Host "  Version:  $version"
Write-Host "  Size:     $sizeText ($($exe.Length) bytes)"
Write-Host "  SHA-256: $hash"
