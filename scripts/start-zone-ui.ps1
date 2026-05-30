param(
    [int]$Port = 8765,
    [switch]$NoBrowser,
    [switch]$Foreground
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = $MyInvocation.MyCommand.Path
$pythonExe = Join-Path $repoRoot ".venv\Scripts\python.exe"
$envFile = Join-Path $repoRoot ".env"

if (-not (Test-Path $pythonExe)) {
    throw "Python environment not found at $pythonExe. Create the venv and install the package first."
}

$url = "http://127.0.0.1:$Port"

if (-not $Foreground) {
    $launchArgs = @(
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $scriptPath,
        "-Port",
        "$Port",
        "-Foreground",
        "-NoBrowser"
    )

    Start-Process -FilePath "powershell" -ArgumentList $launchArgs -WorkingDirectory $repoRoot -WindowStyle Hidden | Out-Null
    if (-not $NoBrowser) {
        Start-Process $url
    }

    Write-Host "Started MatterLights zone designer in the background at $url"
    return
}

Push-Location $repoRoot
try {
    if (-not (Test-Path $envFile)) {
        throw "Missing .env at $envFile. Run guided setup first or create the file manually."
    }

    $env:ZONE_UI_PORT = "$Port"
    & $pythonExe -m matterlights.zone_ui
}
finally {
    Pop-Location
}