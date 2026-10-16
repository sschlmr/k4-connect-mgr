<#
    Build the Windows server installer.

        powershell -ExecutionPolicy Bypass -File packaging\windows-server\build.ps1

    Requires: Python 3.11+, and Inno Setup 6 (iscc.exe) on PATH or in its
    default location. Run from the project root.
#>

param(
    [string]$Version = "0.2.0"
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $root

Write-Host "==> Installing build dependencies"
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

Write-Host "==> Building the server executable with PyInstaller"
python -m PyInstaller --clean --noconfirm packaging\windows-server\k4cm-server.spec

Write-Host "==> Building the installer with Inno Setup"
$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidate = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) {
        $iscc = $candidate
    } else {
        throw "Inno Setup 6 not found. Install it from https://jrsoftware.org/isinfo.php"
    }
} else {
    $iscc = $iscc.Source
}

& $iscc "/DAppVersion=$Version" "packaging\windows-server\installer.iss"

Write-Host ""
Write-Host "Built: packaging\out\k4cm-server-setup.exe"
