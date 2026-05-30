param(
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

if (-not $Foreground) {
    $launchArgs = @(
        "-NoProfile",
        "-WindowStyle",
        "Hidden",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        $scriptPath,
        "-Foreground"
    )

    Start-Process -FilePath "powershell" -ArgumentList $launchArgs -WorkingDirectory $repoRoot -WindowStyle Hidden | Out-Null
    Write-Host "Started MatterLights screen sync in the background"
    return
}

Push-Location $repoRoot
try {
    if (-not (Test-Path $envFile)) {
        throw "Missing .env at $envFile. Run guided setup first or create the file manually."
    }

    & $pythonExe -m matterlights
}
finally {
    Pop-Location
}