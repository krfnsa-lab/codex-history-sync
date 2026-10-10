#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ConfigPath,
    [string]$PythonPath,
    [switch]$NonInteractive,
    [switch]$Force,
    [string]$Distro,
    [string]$WslUser,
    [string]$WindowsHome,
    [string]$WslHome,
    [string]$RuntimeDirectory,
    [string]$TaskName,
    [string]$PythonWsl,
    [string]$IntervalMinutes,
    [string]$QuietSeconds,
    [string]$MaxTransfersPerRun,
    [string]$RunBudgetSeconds,
    [string]$OperationTimeoutSeconds,
    [string]$TaskTimeLimitMinutes
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $PSScriptRoot 'config.json' }
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-SyncLocalDrivePath $PSScriptRoot 'Program directory'
$syncProfile = Get-SyncProfile $ConfigPath -AllowMissingConfiguration
$syncPython = Resolve-SyncPython $syncProfile $PythonPath
$syncArguments = @((Join-Path $PSScriptRoot 'setup.py'), '--config', $syncProfile.ConfigPath, '--python-windows', $syncPython)
if ($NonInteractive) { $syncArguments += '--non-interactive' }
if ($Force) { $syncArguments += '--force' }
foreach ($syncOption in @(
    @{Name='--distro'; Value=$Distro}, @{Name='--wsl-user'; Value=$WslUser},
    @{Name='--windows-home'; Value=$WindowsHome}, @{Name='--wsl-home'; Value=$WslHome},
    @{Name='--runtime-directory'; Value=$RuntimeDirectory}, @{Name='--task-name'; Value=$TaskName},
    @{Name='--python-wsl'; Value=$PythonWsl}, @{Name='--interval-minutes'; Value=$IntervalMinutes},
    @{Name='--quiet-seconds'; Value=$QuietSeconds}, @{Name='--max-transfers-per-run'; Value=$MaxTransfersPerRun},
    @{Name='--run-budget-seconds'; Value=$RunBudgetSeconds}, @{Name='--operation-timeout-seconds'; Value=$OperationTimeoutSeconds},
    @{Name='--task-time-limit-minutes'; Value=$TaskTimeLimitMinutes}
)) {
    if (-not [string]::IsNullOrWhiteSpace($syncOption.Value)) { $syncArguments += @($syncOption.Name, $syncOption.Value) }
}
Invoke-SyncPython $syncPython $syncArguments
