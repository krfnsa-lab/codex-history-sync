#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$PythonPath,
    [ValidateSet('windows', 'wsl')][string]$Side = 'wsl',
    [string[]]$ThreadId,
    [switch]$DryRun,
    [switch]$Json
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $PSScriptRoot 'config.json' }
. (Join-Path $PSScriptRoot 'Common.ps1')
$syncProfile = Get-SyncProfile $ConfigPath
$syncPython = Resolve-SyncPython $syncProfile $PythonPath
$syncArguments = (Get-SyncControllerArguments $syncProfile $syncPython $PSScriptRoot) + @('--repair-paths', '--repair-side', $Side)
foreach ($syncId in $ThreadId) { $syncArguments += @('--id', $syncId) }
if ($DryRun) { $syncArguments += '--dry-run' }
if ($Json) { $syncArguments += '--json' }
Invoke-SyncPython $syncPython $syncArguments
