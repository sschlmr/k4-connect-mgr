<#
    Build the Windows client installer.

        powershell -ExecutionPolicy Bypass -File packaging\windows-client\build.ps1

    Requires: Python 3.11+, and Inno Setup 6 (iscc.exe). Run from the project root.
#>

param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $root

Write-Host "==> Installing build dependencies"
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

Write-Host "==> Building the client executable with PyInstaller"
python -m PyInstaller --clean --noconfirm packaging\windows-client\k4cm.spec

Write-Host "==> Building the installer with Inno Setup"
$iscc = Get-Command iscc.exe -ErrorAction SilentlyContinue
if (-not $iscc) {
    $candidate = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
    if (Test-Path $candidate) { $iscc = $candidate } else {
        throw "Inno Setup 6 not found. Install it from https://jrsoftware.org/isinfo.php"
    }
} else {
    $iscc = $iscc.Source
}

& $iscc "/DAppVersion=$Version" "packaging\windows-client\installer.iss"

Write-Host ""
Write-Host "Built: packaging\out\k4cm-client-setup.exe"
