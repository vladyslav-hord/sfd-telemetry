param(
    [string]$GameDirectory = 'D:\Programs\Steam\steamapps\common\Superfighters Deluxe'
)

if ($PSVersionTable.PSEdition -ne 'Core') {
    throw 'SFD 1.6 uses modern .NET. Run this check with pwsh (PowerShell 7+), not powershell.exe 5.1.'
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $projectRoot 'sfd\SFDTelemetry.txt'
$gameAssembly = Join-Path $GameDirectory 'Superfighters Deluxe.dll'
if (-not (Test-Path -LiteralPath $gameAssembly)) { throw "SFD assembly not found: $gameAssembly" }

$assembly = [Reflection.Assembly]::LoadFrom($gameAssembly)
$gameWorld = $assembly.GetType('SFD.GameWorld')
$flags = [Reflection.BindingFlags]'NonPublic,Static'
$header = [string]$gameWorld.GetField('SCRIPT_HEADER', $flags).GetValue($null)
$footer = [string]$gameWorld.GetField('SCRIPT_FOOTER', $flags).GetValue($null)
$source = [IO.File]::ReadAllText($scriptPath)
$tempDirectory = Join-Path ([IO.Path]::GetTempPath()) ('sfd-telemetry-compile-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDirectory | Out-Null
$output = Join-Path $tempDirectory 'SFDTelemetry.dll'
try {
    $compiler = $gameWorld.GetMethod('CompileScript', $flags)
    [string]$fullScript = $header + "`r`n" + $source + "`r`n" + $footer
    [object[]]$arguments = @($fullScript, [string]$output, [bool]$false)
    $result = $compiler.Invoke($null, $arguments)
    foreach ($diagnostic in $result.Errors) {
        Write-Host "line=$($diagnostic.Line) $($diagnostic.ErrorNumber) $($diagnostic.ErrorText)"
    }
    if ($result.HasErrors) { exit 1 }
    Write-Host 'SFDTelemetry.txt compiles with the installed SFD Script Engine.'
}
finally {
    if ((Test-Path -LiteralPath $tempDirectory) -and $tempDirectory.StartsWith([IO.Path]::GetTempPath(), [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $tempDirectory -Recurse -Force
    }
}
