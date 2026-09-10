param(
    [int]$Port = 8765,
    [switch]$NoBrowser,
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

$url = "http://127.0.0.1:$Port"

if (-not $Foreground) {
    # pythonw, not a hidden PowerShell: Windows Terminal ignores -WindowStyle
    # Hidden and shows the window anyway. pythonw never has a console, and it
    # inherits ZONE_UI_PORT from this process.
    $env:ZONE_UI_PORT = "$Port"
    Start-Process -FilePath $pythonwExe -ArgumentList "-m", "matterlights.zone_ui" -WorkingDirectory $repoRoot | Out-Null
    if (-not $NoBrowser) {
        Start-Process $url
    }

    Write-Host "Started MatterLights zone designer in the background at $url"
    return
}

Push-Location $repoRoot
try {
    $env:ZONE_UI_PORT = "$Port"
    & $pythonExe -m matterlights.zone_ui
}
finally {
    Pop-Location
}
