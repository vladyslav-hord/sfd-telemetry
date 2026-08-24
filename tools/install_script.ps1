param(
    [string]$SfdDocuments = (Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Superfighters Deluxe')
)

$projectRoot = Split-Path -Parent $PSScriptRoot
$source = Join-Path $projectRoot 'sfd\SFDTelemetry.txt'
$targetDirectory = Join-Path $SfdDocuments 'Scripts\Custom'
$target = Join-Path $targetDirectory 'BotRotation.txt'

if (-not (Test-Path -LiteralPath $source)) {
    throw "Telemetry script not found: $source"
}
New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $target -Force
Write-Host "Installed: $target"
Write-Host 'Enable Custom\BotRotation.txt once in Server Tool -> Scripts.'
