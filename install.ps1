#Requires -Version 5.1
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [string]$ConfigPath,
    [string]$PythonPath
)
$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($ConfigPath)) { $ConfigPath = Join-Path $PSScriptRoot 'config.json' }
. (Join-Path $PSScriptRoot 'Common.ps1')
Assert-SyncLocalDrivePath $PSScriptRoot 'Program directory'
$syncProfile = Get-SyncProfile $ConfigPath
Assert-SyncConfiguration $syncProfile
$syncPython = Resolve-SyncPython $syncProfile $PythonPath
$syncPythonw = Resolve-SyncPythonw $syncPython
$syncController = Join-Path $PSScriptRoot 'history_sync.py'
if (-not (Test-Path -LiteralPath $syncController -PathType Leaf)) { throw 'history_sync.py is missing.' }
$syncLedgerPath = Join-Path $syncProfile.RuntimeDirectory 'ledger.json'
if (Test-Path -LiteralPath (Join-Path $syncProfile.RuntimeDirectory 'pending.json')) {
    throw 'An unfinished import exists. Review pending.json before installing.'
}
if (-not (Test-Path -LiteralPath $syncLedgerPath -PathType Leaf)) {
    throw 'The profile baseline is missing. Run sync-now.ps1 -Initialize with this same -ConfigPath first.'
}
try { $syncLedger = Get-Content -LiteralPath $syncLedgerPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop }
catch { throw 'The profile ledger is not valid JSON. Preserve it and inspect it before installing.' }
if ($null -eq $syncLedger -or $syncLedger.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
    throw 'The profile ledger must be a JSON object.'
}
$syncVersion = $syncLedger.PSObject.Properties['version']
$syncThreads = $syncLedger.PSObject.Properties['threads']
if ($null -eq $syncVersion -or $syncVersion.Value -is [bool] -or
    $syncVersion.Value -isnot [System.ValueType] -or $syncVersion.Value -ne 1 -or
    $null -eq $syncThreads -or $null -eq $syncThreads.Value -or
    $syncThreads.Value.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
    throw 'The profile ledger needs numeric version 1 and a threads JSON object.'
}
$syncInterval = Get-SyncPositiveInteger $syncProfile.Configuration 'interval_minutes' 5
$syncHardMinutes = Get-SyncPositiveInteger $syncProfile.Configuration 'task_time_limit_minutes' 10
$syncBudget = Get-SyncPositiveInteger $syncProfile.Configuration 'run_budget_seconds' 120
$syncOperationTimeout = Get-SyncPositiveInteger $syncProfile.Configuration 'operation_timeout_seconds' 120
$syncMinimumSeconds = [decimal]$syncBudget + 3 * [decimal]$syncOperationTimeout + 60
if ([decimal]$syncHardMinutes * 60 -lt $syncMinimumSeconds) {
    throw 'task_time_limit_minutes must allow run_budget_seconds + 3 * operation_timeout_seconds + 60 seconds.'
}
if ($syncInterval -gt 44640) { throw 'interval_minutes cannot exceed 31 days (44640 minutes).' }
$syncTaskPath = '\'
$syncExisting = @(Get-ScheduledTask -TaskPath $syncTaskPath -ErrorAction Stop | Where-Object { $_.TaskName -eq $syncProfile.TaskName })
if ($syncExisting.Count -gt 1) { throw 'Unexpected duplicate scheduled tasks were returned.' }
if ($syncExisting.Count -eq 1) { Assert-SyncTaskOwnership $syncExisting[0] $syncProfile $PSScriptRoot }
$syncIdentity = Get-SyncTaskIdentity $syncProfile $syncPython $PSScriptRoot
$syncCurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$syncFirstStart = (Get-Date).AddMinutes($syncInterval)
$syncAction = New-ScheduledTaskAction -Execute $syncPythonw -Argument $syncIdentity.Arguments -WorkingDirectory $PSScriptRoot
$syncPeriodic = New-ScheduledTaskTrigger -Once -At $syncFirstStart -RepetitionInterval (New-TimeSpan -Minutes $syncInterval)
$syncAtLogon = New-ScheduledTaskTrigger -AtLogOn -User $syncCurrentUser
$syncPrincipal = New-ScheduledTaskPrincipal -UserId $syncCurrentUser -LogonType Interactive -RunLevel Limited
$syncSettings = New-ScheduledTaskSettingsSet -Compatibility Win8 -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes $syncHardMinutes) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$syncSettings.WakeToRun = $false
$syncDefinition = New-ScheduledTask -Action $syncAction -Trigger @($syncPeriodic, $syncAtLogon) `
    -Principal $syncPrincipal -Settings $syncSettings -Description $syncIdentity.Description
[pscustomobject]@{
    TaskName=$syncProfile.TaskName; ConfigPath=$syncProfile.ConfigPath; RuntimeDirectory=$syncProfile.RuntimeDirectory
    Python=$syncPython; Pythonw=$syncPythonw; Arguments=$syncIdentity.Arguments; User=$syncCurrentUser
    FirstScheduledRun=$syncFirstStart; IntervalMinutes=$syncInterval; TimeLimitMinutes=$syncHardMinutes
    RunAtLogon=$true; RequiresUserLoggedOn=$true; RunLevel='Limited'; MultipleInstances='IgnoreNew'; WakeComputer=$false
} | Format-List | Out-Host
if ($PSCmdlet.ShouldProcess("$syncTaskPath$($syncProfile.TaskName)", 'Register or update this public installation/profile''s sync task')) {
    Register-ScheduledTask -TaskName $syncProfile.TaskName -TaskPath $syncTaskPath -InputObject $syncDefinition -Force -ErrorAction Stop | Out-Null
    Write-Host 'Installed for the current logged-on user. No password was stored.'
}
