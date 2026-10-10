#Requires -Version 5.1
# Shared Windows-only configuration and interpreter helpers. No history writes.
Set-StrictMode -Version Latest

function Resolve-SyncAbsolutePath {
    param([Parameter(Mandatory = $true)][string]$Value, [string]$BaseDirectory = (Get-Location).ProviderPath)
    $syncExpanded = [Environment]::ExpandEnvironmentVariables($Value)
    if ($syncExpanded -match '^~(?:[\\/]|$)') {
        $syncUserProfile = [Environment]::GetFolderPath('UserProfile')
        $syncUserTail = $syncExpanded.Substring(1).TrimStart([char[]]'\/')
        $syncExpanded = $syncUserProfile
        if ($syncUserTail) { $syncExpanded = Join-Path $syncUserProfile $syncUserTail }
    }
    if ($syncExpanded -match '^[A-Za-z]:[^\\/]') {
        throw 'Drive-relative paths are unsupported; use an absolute path or a normal relative path.'
    }
    if (-not [System.IO.Path]::IsPathRooted($syncExpanded)) {
        $syncExpanded = Join-Path $BaseDirectory $syncExpanded
    }
    return [System.IO.Path]::GetFullPath($syncExpanded)
}

function Assert-SyncLocalDrivePath {
    param([string]$Value, [string]$Name = 'path')
    if ($Value -notmatch '^[A-Za-z]:[\\/]') {
        throw "$Name must be on a native local Windows drive, not a UNC or WSL share."
    }
    $syncDriveRoot = [System.IO.Path]::GetPathRoot($Value)
    $syncDrive = New-Object System.IO.DriveInfo($syncDriveRoot)
    if ($syncDrive.DriveType -in @([System.IO.DriveType]::Network, [System.IO.DriveType]::Unknown, [System.IO.DriveType]::NoRootDirectory)) {
        throw "$Name must use an existing local Windows drive, not a mapped network drive."
    }
}

function Get-SyncProfile {
    param([Parameter(Mandatory = $true)][string]$ConfigPath, [switch]$AllowMissingConfiguration)
    $syncConfigPath = Resolve-SyncAbsolutePath $ConfigPath
    $syncConfigDirectory = Split-Path $syncConfigPath -Parent
    $syncConfiguration = [pscustomobject]@{}
    if (Test-Path -LiteralPath $syncConfigPath -PathType Leaf) {
        try {
            $syncConfiguration = Get-Content -LiteralPath $syncConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json -ErrorAction Stop
        } catch { throw "Configuration is not valid JSON: $syncConfigPath" }
        if ($null -eq $syncConfiguration -or $syncConfiguration.GetType().FullName -cne 'System.Management.Automation.PSCustomObject') {
            throw 'Configuration must contain a JSON object.'
        }
    } elseif (-not $AllowMissingConfiguration) {
        throw 'Configuration is missing. Run configure.ps1 first or provide -ConfigPath.'
    }
    $syncRuntimeProperty = $syncConfiguration.PSObject.Properties['runtime_directory']
    $syncRuntimeValue = 'runtime'
    if ($null -ne $syncRuntimeProperty) {
        if ($syncRuntimeProperty.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($syncRuntimeProperty.Value)) {
            throw 'runtime_directory must be a non-empty string.'
        }
        $syncRuntimeValue = $syncRuntimeProperty.Value
    }
    $syncTaskProperty = $syncConfiguration.PSObject.Properties['task_name']
    $syncTaskName = 'CodexHistorySync-Public'
    if ($null -ne $syncTaskProperty) {
        if ($syncTaskProperty.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($syncTaskProperty.Value)) {
            throw 'task_name must be a non-empty string.'
        }
        $syncTaskName = $syncTaskProperty.Value
    }
    if ($syncTaskName -match '[\\/:*?"<>|\x00-\x1f\x7f]' -or $syncTaskName.Length -gt 200) {
        throw 'task_name must be a single task name without reserved characters or ASCII controls (at most 200 characters).'
    }
    $syncResolvedRuntime = Resolve-SyncAbsolutePath $syncRuntimeValue $syncConfigDirectory
    Assert-SyncLocalDrivePath $syncResolvedRuntime 'runtime_directory'
    return [pscustomobject]@{
        ConfigPath = $syncConfigPath
        ConfigDirectory = $syncConfigDirectory
        Configuration = $syncConfiguration
        RuntimeDirectory = $syncResolvedRuntime
        TaskName = $syncTaskName
    }
}

function Get-SyncPositiveInteger {
    param($Configuration, [string]$Name, [long]$Default)
    $syncProperty = $Configuration.PSObject.Properties[$Name]
    if ($null -eq $syncProperty) { return $Default }
    $syncValue = $syncProperty.Value
    if ($null -eq $syncValue -or
        $syncValue.GetType().FullName -notin @('System.Int32', 'System.Int64', 'System.Numerics.BigInteger') -or
        $syncValue -le 0) {
        throw "$Name must be a positive integer when provided."
    }
    if ($syncValue -gt [long]::MaxValue) { throw "$Name exceeds the supported integer range." }
    return [long]$syncValue
}

function Assert-SyncConfiguration {
    param($Profile)
    foreach ($syncKey in @('windows_home', 'wsl_distro', 'wsl_user', 'wsl_home', 'python_windows', 'python_wsl')) {
        $syncProperty = $Profile.Configuration.PSObject.Properties[$syncKey]
        if ($null -eq $syncProperty -or $syncProperty.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($syncProperty.Value)) {
            throw "Configuration requires a non-empty string for $syncKey."
        }
    }
    foreach ($syncSetting in @(
        @{Name='quiet_seconds'; Default=60}, @{Name='operation_timeout_seconds'; Default=120},
        @{Name='max_transfers_per_run'; Default=10}, @{Name='run_budget_seconds'; Default=120},
        @{Name='interval_minutes'; Default=5}, @{Name='task_time_limit_minutes'; Default=10}
    )) {
        Get-SyncPositiveInteger $Profile.Configuration $syncSetting.Name $syncSetting.Default | Out-Null
    }
}

function ConvertTo-SyncNativeArgument {
    param([AllowEmptyString()][string]$Value)
    $syncEscaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $syncEscaped = [regex]::Replace($syncEscaped, '(\\+)$', '$1$1')
    return '"' + $syncEscaped + '"'
}

function Invoke-SyncProbe {
    param([string]$Executable, [string[]]$ArgumentList)
    $syncStartInfo = New-Object System.Diagnostics.ProcessStartInfo
    $syncStartInfo.FileName = $Executable
    $syncStartInfo.Arguments = (($ArgumentList | ForEach-Object { ConvertTo-SyncNativeArgument $_ }) -join ' ')
    $syncStartInfo.UseShellExecute = $false
    $syncStartInfo.CreateNoWindow = $true
    $syncStartInfo.RedirectStandardOutput = $true
    $syncStartInfo.RedirectStandardError = $true
    $syncStartInfo.StandardOutputEncoding = New-Object System.Text.UTF8Encoding($false)
    $syncStartInfo.StandardErrorEncoding = New-Object System.Text.UTF8Encoding($false)
    $syncProcess = New-Object System.Diagnostics.Process
    $syncProcess.StartInfo = $syncStartInfo
    try {
        if (-not $syncProcess.Start()) { throw 'Unable to start the interpreter probe.' }
        if (-not $syncProcess.WaitForExit(10000)) {
            $syncProcess.Kill()
            throw 'Python detection timed out.'
        }
        if ($syncProcess.ExitCode -ne 0) { throw 'The Python interpreter probe failed.' }
        $syncLines = @($syncProcess.StandardOutput.ReadToEnd().Trim() -split '\r?\n')
        $syncInformation = $syncLines[-1] | ConvertFrom-Json -ErrorAction Stop
        if ($syncInformation.implementation -cne 'cpython' -or $syncInformation.platform -cne 'win32' -or
            $syncInformation.version[0] -ne 3 -or $syncInformation.version[1] -lt 11) {
            throw 'A native Windows CPython 3.11 or newer is required.'
        }
        $syncResolved = Resolve-SyncAbsolutePath $syncInformation.executable
        if (-not (Test-Path -LiteralPath $syncResolved -PathType Leaf)) { throw 'Python reported a missing executable.' }
        return $syncResolved
    } finally { $syncProcess.Dispose() }
}

function Resolve-SyncPython {
    param($Profile, [string]$PythonPath)
    $syncProbeCode = 'import json,sys; print(json.dumps({"executable":sys.executable,"version":list(sys.version_info[:3]),"implementation":sys.implementation.name,"platform":sys.platform},ensure_ascii=True))'
    $syncRequested = $PythonPath
    $syncBase = (Get-Location).ProviderPath
    if ([string]::IsNullOrWhiteSpace($syncRequested)) {
        $syncProperty = $Profile.Configuration.PSObject.Properties['python_windows']
        if ($null -ne $syncProperty) {
            if ($syncProperty.Value -isnot [string] -or [string]::IsNullOrWhiteSpace($syncProperty.Value)) {
                throw 'python_windows must be a non-empty string or auto.'
            }
            $syncRequested = $syncProperty.Value
            $syncBase = $Profile.ConfigDirectory
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($syncRequested) -and $syncRequested -ine 'auto') {
        $syncCandidate = Resolve-SyncAbsolutePath $syncRequested $syncBase
        if (-not (Test-Path -LiteralPath $syncCandidate -PathType Leaf)) {
            throw 'The requested/configured Python does not exist. Provide -PythonPath or run configure.ps1 again.'
        }
        return Invoke-SyncProbe $syncCandidate @('-X', 'utf8', '-c', $syncProbeCode)
    }
    $syncCandidates = New-Object System.Collections.Generic.List[string]
    foreach ($syncCommandName in @('python.exe', 'python3.exe', 'py.exe')) {
        foreach ($syncCommand in @(Get-Command $syncCommandName -CommandType Application -ErrorAction SilentlyContinue)) {
            if ($syncCommand.Source -notmatch '\\Microsoft\\WindowsApps\\') { $syncCandidates.Add($syncCommand.Source) }
        }
    }
    $syncLocalPrograms = Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'Programs\Python'
    foreach ($syncFolder in @(Get-ChildItem -LiteralPath $syncLocalPrograms -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending)) {
        $syncCandidate = Join-Path $syncFolder.FullName 'python.exe'
        if (Test-Path -LiteralPath $syncCandidate -PathType Leaf) { $syncCandidates.Add($syncCandidate) }
    }
    foreach ($syncCandidate in @($syncCandidates | Select-Object -Unique)) {
        try {
            $syncArguments = @('-X', 'utf8', '-c', $syncProbeCode)
            if ([System.IO.Path]::GetFileName($syncCandidate) -ieq 'py.exe') { $syncArguments = @('-3') + $syncArguments }
            return Invoke-SyncProbe $syncCandidate $syncArguments
        } catch { continue }
    }
    throw 'No native Windows CPython 3.11+ was found. Install Python or supply -PythonPath.'
}

function Resolve-SyncPythonw {
    param([string]$PythonPath)
    $syncPythonw = Join-Path (Split-Path $PythonPath -Parent) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $syncPythonw -PathType Leaf)) {
        throw 'pythonw.exe is missing beside the selected Python. Use a standard Windows CPython installation.'
    }
    return (Resolve-Path -LiteralPath $syncPythonw).ProviderPath
}

function Invoke-SyncPython {
    param([string]$PythonPath, [string[]]$ArgumentList)
    $syncPriorEncoding = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        & $PythonPath -X utf8 @ArgumentList
        $syncResultCode = $LASTEXITCODE
    } finally { [Console]::OutputEncoding = $syncPriorEncoding }
    if ($syncResultCode -ne 0) { throw "The command exited with code $syncResultCode. Review its output above." }
}

function Get-SyncControllerArguments {
    param($Profile, [string]$PythonPath, [string]$ScriptDirectory)
    Assert-SyncLocalDrivePath $ScriptDirectory 'Program directory'
    return @((Join-Path $ScriptDirectory 'history_sync.py'), '--config', $Profile.ConfigPath,
        '--runtime-dir', $Profile.RuntimeDirectory, '--task-name', $Profile.TaskName,
        '--python-windows', $PythonPath)
}

function Get-SyncTaskIdentity {
    param($Profile, [string]$PythonPath, [string]$ScriptDirectory)
    $syncArguments = @('-X', 'utf8') + (Get-SyncControllerArguments $Profile $PythonPath $ScriptDirectory) + @('--once')
    return [pscustomobject]@{
        Description = "CodexHistorySync public v2; directory=$ScriptDirectory; config=$($Profile.ConfigPath); runtime=$($Profile.RuntimeDirectory)"
        Arguments = (($syncArguments | ForEach-Object { ConvertTo-SyncNativeArgument $_ }) -join ' ')
        WorkingDirectory = $ScriptDirectory
    }
}

function Assert-SyncTaskOwnership {
    param($Task, $Profile, [string]$ScriptDirectory)
    $syncActions = @($Task.Actions)
    if ($syncActions.Count -ne 1 -or $syncActions[0].Arguments -notmatch ' "--python-windows" "([^\"]+)" "--once"$') {
        throw 'Refusing to change a task whose action is not owned by this installation/profile.'
    }
    $syncOriginalPython = $Matches[1]
    $syncIdentity = Get-SyncTaskIdentity $Profile $syncOriginalPython $ScriptDirectory
    $syncOriginalPythonw = Join-Path (Split-Path $syncOriginalPython -Parent) 'pythonw.exe'
    if ($Task.Description -cne $syncIdentity.Description -or
        $syncActions[0].Arguments -cne $syncIdentity.Arguments -or
        $syncActions[0].Execute -ine $syncOriginalPythonw -or
        $syncActions[0].WorkingDirectory -ine $ScriptDirectory) {
        throw 'Refusing to change a task owned by another directory, configuration or runtime profile.'
    }
}
