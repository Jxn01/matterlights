param(
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
$pythonwExe = Join-Path $repoRoot ".venv\Scripts\pythonw.exe"
$envFile = Join-Path $repoRoot ".env"

if (-not (Test-Path $pythonExe)) {
    throw "Python environment not found at $pythonExe. Create the venv and install the package first."
}

if (-not (Test-Path $envFile)) {
    throw "Missing .env at $envFile. Run guided setup first or create the file manually."
}

if (-not $Foreground) {
    # pythonw, not a hidden PowerShell: Windows Terminal ignores -WindowStyle
    # Hidden and shows the window anyway. pythonw never has a console; its
    # errors go to the log -- LOG_PATH in .env, or
    # %LOCALAPPDATA%\matterlights\matterlights.log.
    Start-Process -FilePath $pythonwExe -ArgumentList "-m", "matterlights" -WorkingDirectory $repoRoot | Out-Null
    Write-Host "Started MatterLights screen sync in the background"
    return
}

Push-Location $repoRoot
try {
    & $pythonExe -m matterlights
}
finally {
    Pop-Location
}
