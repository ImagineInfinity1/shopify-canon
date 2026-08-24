$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPath = Join-Path $RepoRoot ".venv-packaging"
$PythonExe = Join-Path $VenvPath "Scripts\python.exe"

if (-not (Test-Path $PythonExe)) {
    py -3.11 -m venv $VenvPath
}

& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r (Join-Path $RepoRoot "requirements.txt")
& $PythonExe -m pip install pyinstaller
& $PythonExe -m PyInstaller --clean --noconfirm (Join-Path $RepoRoot "packaging\pyinstaller\listing_cannon_psd_framer.spec")

Write-Host ""
Write-Host "Built app folder:"
Write-Host (Join-Path $RepoRoot "dist\Listing Cannon PSD Framer")
Write-Host ""
Write-Host "To build the installer, open packaging\inno\listing_cannon_psd_framer.iss in Inno Setup and compile it."
