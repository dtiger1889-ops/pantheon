# Run by the Scheduled Task \Pantheon\OpenEditor in the user's desktop session.
# Reads the VS Code command line the deck wrote (pantheon/editor_bridge.py) and runs it.
# Only a code / code.cmd / Code.exe command is run; anything else in the file is ignored.
param(
    [string]$Pending = "$PSScriptRoot\..\state\editor\pending_open.json"
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Pending)) { exit 0 }
$request = Get-Content -LiteralPath $Pending -Raw | ConvertFrom-Json
Remove-Item -LiteralPath $Pending -Force
$argv = @($request.argv)
if ($argv.Count -lt 2) { exit 1 }
$exe = [string]$argv[0]
$leaf = [System.IO.Path]::GetFileName($exe).ToLowerInvariant()
if (@('code', 'code.cmd', 'code.exe') -notcontains $leaf) { exit 1 }
if (-not (Test-Path -LiteralPath $exe)) { exit 1 }
$rest = @($argv | Select-Object -Skip 1 | ForEach-Object { [string]$_ })
& $exe @rest
