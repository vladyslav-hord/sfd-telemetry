param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('hourly')]
    [string]$Mode
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$exitCode = 1

$keyPath = Join-Path $root 'OPENAI_API'
if (Test-Path $keyPath) {
    $env:OPENAI_API_KEY = (Get-Content -Raw $keyPath).Trim()
}
& py -3 -m analyzer.main $Mode --config analyzer\config.json
$exitCode = $LASTEXITCODE
exit $exitCode
