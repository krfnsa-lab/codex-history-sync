#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$PythonPath,
    [switch]$Json
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $PSScriptRoot 'config.json' }
. (Join-Path $PSScriptRoot 'Common.ps1')
$syncProfile = Get-SyncProfile $ConfigPath -AllowMissingConfiguration
$syncPython = Resolve-SyncPython $syncProfile $PythonPath
$syncArguments = (Get-SyncControllerArguments $syncProfile $syncPython $PSScriptRoot) + @('--status')
if ($Json) { $syncArguments += '--json' }
Invoke-SyncPython $syncPython $syncArguments
