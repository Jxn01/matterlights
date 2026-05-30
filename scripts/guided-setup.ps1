param(
    [string]$HaUrl = "http://192.168.1.2:8123"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$envExample = Join-Path $repoRoot ".env.example"
$envFile = Join-Path $repoRoot ".env"

function Read-EnvFile {
    param([string]$Path)

    $map = [ordered]@{}
    if (-not (Test-Path $Path)) {
        return $map
    }

    foreach ($line in Get-Content $Path) {
        if ([string]::IsNullOrWhiteSpace($line) -or $line.TrimStart().StartsWith("#")) {
            continue
        }

        $parts = $line -split "=", 2
        if ($parts.Count -eq 2) {
            $map[$parts[0].Trim()] = $parts[1].Trim()
        }
    }

    return $map
}

function Write-EnvFile {
    param(
        [string]$Path,
        [hashtable]$Values
    )

    $lines = @()
    foreach ($key in $Values.Keys) {
        $lines += "$key=$($Values[$key])"
    }
    Set-Content -Path $Path -Value $lines -Encoding utf8
}

function Set-EnvValue {
    param(
        [hashtable]$Values,
        [string]$Key,
        [string]$Value
    )

    $Values[$Key] = $Value
}

function Get-PlaintextFromSecureString {
    param([securestring]$Value)

    return [System.Net.NetworkCredential]::new("", $Value).Password
}

function Prompt-ForToken {
    param([string]$Url)

    Write-Host "Open Home Assistant, then go to your profile page and create a Long-Lived Access Token."
    Write-Host "Direct URL: $Url/profile"
    $tokenSecure = Read-Host "Paste the token here" -AsSecureString
    $token = (Get-PlaintextFromSecureString -Value $tokenSecure).Trim()
    if ([string]::IsNullOrWhiteSpace($token)) {
        throw "A Home Assistant token is required."
    }
    return $token
}

Push-Location $repoRoot
try {
    if (-not (Test-Path $venvPython)) {
        Write-Host "Creating Python virtual environment..."
        py -3.12 -m venv .venv
    }

    Write-Host "Installing package and dependencies..."
    & $venvPython -m pip install -e . | Out-Host

    if (-not (Test-Path $envFile)) {
        Copy-Item $envExample $envFile
    }

    $envValues = Read-EnvFile -Path $envFile
    Set-EnvValue -Values $envValues -Key "HA_URL" -Value $HaUrl

    $currentToken = [string]($envValues["HA_TOKEN"])
    if ([string]::IsNullOrWhiteSpace($currentToken) -or $currentToken -like "replace-*") {
        $token = Prompt-ForToken -Url $HaUrl
        Set-EnvValue -Values $envValues -Key "HA_TOKEN" -Value $token
        Write-EnvFile -Path $envFile -Values $envValues
    }

    Write-Host ""
    while ($true) {
        Write-Host "Discovering Home Assistant light entities..."
        & $venvPython -m matterlights.discover | Out-Host
        if ($LASTEXITCODE -eq 0) {
            break
        }

        if ($LASTEXITCODE -eq 2) {
            throw "No Home Assistant light entities were found. Add your bulbs to Home Assistant first, then rerun this setup."
        }

        $retryToken = Read-Host "Home Assistant rejected the token or URL. Enter a new token? (Y/n)"
        if (-not [string]::IsNullOrWhiteSpace($retryToken) -and $retryToken -notmatch '^(y|yes)$') {
            throw "Stopping setup because Home Assistant authentication failed."
        }

        $token = Prompt-ForToken -Url $HaUrl
        Set-EnvValue -Values $envValues -Key "HA_TOKEN" -Value $token
        Write-EnvFile -Path $envFile -Values $envValues
        Write-Host ""
    }

    $currentEntities = [string]($envValues["HA_LIGHT_ENTITIES"])
    $entitiesPrompt = "Paste the light entity IDs to sync, comma-separated"
    if (-not [string]::IsNullOrWhiteSpace($currentEntities) -and $currentEntities -notlike "light.example*") {
        $entitiesPrompt = "$entitiesPrompt`nPress Enter to keep: $currentEntities"
    }

    $chosenEntities = Read-Host $entitiesPrompt
    if ([string]::IsNullOrWhiteSpace($chosenEntities)) {
        $chosenEntities = $currentEntities
    }
    if ([string]::IsNullOrWhiteSpace($chosenEntities) -or $chosenEntities -like "light.example*") {
        throw "You must provide at least one real light entity ID."
    }

    Set-EnvValue -Values $envValues -Key "HA_LIGHT_ENTITIES" -Value $chosenEntities
    Write-EnvFile -Path $envFile -Values $envValues

    $installAutostart = Read-Host "Install automatic startup at Windows logon? (y/N)"
    if ($installAutostart -match '^(y|yes)$') {
        & (Join-Path $PSScriptRoot "install-autostart.ps1") | Out-Host
    }

    $startNow = Read-Host "Start syncing now? (Y/n)"
    if ([string]::IsNullOrWhiteSpace($startNow) -or $startNow -match '^(y|yes)$') {
        Write-Host "Starting sync. Leave this window open while you want syncing to continue."
        & $venvPython -m matterlights
    }
    else {
        Write-Host "Setup finished. Start later with: .\\.venv\\Scripts\\python.exe -m matterlights"
    }
}
finally {
    Pop-Location
}