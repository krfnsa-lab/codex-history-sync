#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $PSScriptRoot 'config.json' }
. (Join-Path $PSScriptRoot 'Common.ps1')
$syncProfile = Get-SyncProfile $ConfigPath -AllowMissingConfiguration
$syncExisting = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -eq $syncProfile.TaskName })
if ($syncExisting.Count -eq 0) { Write-Host 'No matching task exists. Nothing was changed.'; return }
if ($syncExisting.Count -ne 1) { throw 'Unexpected duplicate scheduled tasks were returned.' }
Assert-SyncTaskOwnership $syncExisting[0] $syncProfile $PSScriptRoot
if ($PSCmdlet.ShouldProcess("\$($syncProfile.TaskName)", 'Remove only this public installation/profile''s scheduled sync task')) {
    Unregister-ScheduledTask -TaskName $syncProfile.TaskName -TaskPath '\' -Confirm:$false -ErrorAction Stop
    Write-Host 'Scheduled task removed. Configuration, baseline, history, logs and backups were preserved.'
}
