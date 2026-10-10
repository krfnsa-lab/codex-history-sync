#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$PythonPath,
    [switch]$DryRun,
    [switch]$Initialize,
    [switch]$Json
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $PSScriptRoot 'config.json' }
. (Join-Path $PSScriptRoot 'Common.ps1')
if ($DryRun -and $Initialize) { throw 'DryRun and Initialize cannot be used together.' }
$syncProfile = Get-SyncProfile $ConfigPath
$syncPython = Resolve-SyncPython $syncProfile $PythonPath
$syncArguments = (Get-SyncControllerArguments $syncProfile $syncPython $PSScriptRoot) + @('--once')
if ($DryRun) { $syncArguments += '--dry-run' }
if ($Initialize) { $syncArguments += '--initialize' }
if ($Json) { $syncArguments += '--json' }
Invoke-SyncPython $syncPython $syncArguments
