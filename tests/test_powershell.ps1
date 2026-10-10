#Requires -Version 5.1
# Standalone Windows tests use dummy endpoints and never query or modify real tasks/history.
[CmdletBinding()]
param([string]$PythonPath)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'Run these PowerShell tests on Windows with native CPython 3.11+.'
}
$syncSource = Split-Path $PSScriptRoot -Parent
$syncTempDirectory = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd([char[]]'\/')
$syncTestLeaf = 'codex-history-sync-public-tests-' + [guid]::NewGuid().ToString('N')
$syncTestRoot = [System.IO.Path]::GetFullPath((Join-Path $syncTempDirectory $syncTestLeaf))
$syncOwnedTestRoot = $syncTestRoot
$syncCleaned = $false
# Load harmless definition constructors before mocks so module auto-loading cannot replace them.
Import-Module ScheduledTasks -ErrorAction Stop
$syncMockState = @{ Tasks=@(); GetCalls=0; RegisterCalls=0; UnregisterCalls=0 }
function Get-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskPath)
    if ($TaskPath -cne '\') { throw 'Tests only expect the scheduler root path.' }
    $syncMockState.GetCalls++
    return $syncMockState.Tasks
}
function Register-ScheduledTask {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath, [object]$InputObject, [switch]$Force)
    $syncMockState.RegisterCalls++
    throw 'Test safety guard: real scheduled-task registration is forbidden.'
}
function Unregister-ScheduledTask {
    [CmdletBinding(SupportsShouldProcess=$true)]
    param([string]$TaskName, [string]$TaskPath)
    $syncMockState.UnregisterCalls++
    throw 'Test safety guard: real scheduled-task removal is forbidden.'
}
try {
    $syncScriptDirectory = Join-Path $syncTestRoot 'application with spaces'
    $syncProfileDirectory = Join-Path $syncTestRoot ('profile ' + [char]0x4e2d + [char]0x6587)
    New-Item -ItemType Directory -Path $syncScriptDirectory, $syncProfileDirectory -Force | Out-Null
    foreach ($syncFile in @('Common.ps1','sync-now.ps1','status.ps1','doctor.ps1','configure.ps1','install.ps1','uninstall.ps1')) {
        Copy-Item -LiteralPath (Join-Path $syncSource $syncFile) -Destination (Join-Path $syncScriptDirectory $syncFile)
        $syncTokens = $null; $syncErrors = $null
        [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $syncScriptDirectory $syncFile), [ref]$syncTokens, [ref]$syncErrors) | Out-Null
        if ($syncErrors.Count) { throw "Syntax error: $syncFile" }
    }
    . (Join-Path $syncScriptDirectory 'Common.ps1')
    $syncBlankProfile = Get-SyncProfile (Join-Path $syncProfileDirectory 'missing.json') -AllowMissingConfiguration
    $syncPython = Resolve-SyncPython $syncBlankProfile $PythonPath
    $syncPythonw = Resolve-SyncPythonw $syncPython
    $syncTestConfigPath = Join-Path $syncProfileDirectory 'config.json'
    $syncTaskName = 'CodexHistorySync-Public-Test-' + [guid]::NewGuid().ToString('N')
    $syncValid = [ordered]@{
        windows_home=(Join-Path $syncTestRoot 'fake-windows-history'); wsl_distro='ExampleDistro'; wsl_user='your-linux-user'; wsl_home='/home/your-linux-user/.codex'
        python_windows=$syncPython; python_wsl='/usr/bin/python3'; runtime_directory='local runtime'; task_name=$syncTaskName
    }
    $syncUtf8 = New-Object System.Text.UTF8Encoding($false)
    function Write-SyncTestJson { param([string]$Path,[object]$Value) [System.IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12 -Compress), $syncUtf8) }
    Write-SyncTestJson $syncTestConfigPath $syncValid
    $syncProfile = Get-SyncProfile $syncTestConfigPath
    New-Item -ItemType Directory -Path $syncProfile.RuntimeDirectory -Force | Out-Null
    $syncLedgerPath = Join-Path $syncProfile.RuntimeDirectory 'ledger.json'
    [System.IO.File]::WriteAllText($syncLedgerPath, '{"version":1,"threads":{}}', $syncUtf8)
    $syncStub = "import json,sys`nif '--json' in sys.argv or sys.argv[0].endswith('setup.py'):`n print(json.dumps({'argv':sys.argv[1:]}))`nelse:`n print(''.join(chr(n) for n in [0x72b6,0x6001,0x6b63,0x5e38]))`n"
    [System.IO.File]::WriteAllText((Join-Path $syncScriptDirectory 'history_sync.py'), $syncStub, $syncUtf8)
    [System.IO.File]::WriteAllText((Join-Path $syncScriptDirectory 'setup.py'), $syncStub, $syncUtf8)
    $syncPassed = 7
    function Assert-SyncTest { param([bool]$Condition,[string]$Name) if (-not $Condition) { throw "FAIL: $Name" }; $script:syncPassed++ }
    function Assert-SyncReject { param([scriptblock]$Action,[string]$Message,[string]$Name)
        $syncRejected=$false
        try { & $Action | Out-Null } catch { if ($_.Exception.Message.Contains($Message)) { $syncRejected=$true } else { throw "Unexpected rejection for ${Name}: $($_.Exception.Message)" } }
        Assert-SyncTest $syncRejected $Name
    }
    function Invoke-SyncTestFile {
        param([string]$ScriptName, [string[]]$ExtraArguments, [string]$WorkingDirectory)
        $syncWindowsPowerShell = Join-Path ([Environment]::GetFolderPath('System')) 'WindowsPowerShell\v1.0\powershell.exe'
        $syncFileArguments = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            (Join-Path $syncScriptDirectory $ScriptName), '-PythonPath', $syncPython) + $ExtraArguments
        $syncFileStartInfo = New-Object System.Diagnostics.ProcessStartInfo
        $syncFileStartInfo.FileName = $syncWindowsPowerShell
        $syncFileStartInfo.Arguments = (($syncFileArguments | ForEach-Object { ConvertTo-SyncNativeArgument $_ }) -join ' ')
        $syncFileStartInfo.WorkingDirectory = $syncScriptDirectory
        if (-not [string]::IsNullOrWhiteSpace($WorkingDirectory)) { $syncFileStartInfo.WorkingDirectory = $WorkingDirectory }
        $syncFileStartInfo.UseShellExecute = $false
        $syncFileStartInfo.CreateNoWindow = $true
        $syncFileStartInfo.RedirectStandardOutput = $true
        $syncFileStartInfo.RedirectStandardError = $true
        $syncFileStartInfo.StandardOutputEncoding = $syncUtf8
        $syncFileStartInfo.StandardErrorEncoding = $syncUtf8
        $syncFileProcess = New-Object System.Diagnostics.Process
        $syncFileProcess.StartInfo = $syncFileStartInfo
        try {
            if (-not $syncFileProcess.Start()) { throw "Failed to start Windows PowerShell for $ScriptName." }
            if (-not $syncFileProcess.WaitForExit(30000)) {
                $syncFileProcess.Kill()
                throw "Windows PowerShell -File timed out for $ScriptName."
            }
            $syncFileOutput = $syncFileProcess.StandardOutput.ReadToEnd()
            $syncFileError = $syncFileProcess.StandardError.ReadToEnd()
            Assert-SyncTest ($syncFileProcess.ExitCode -eq 0) "$ScriptName native -File succeeds without ConfigPath: $syncFileError"
            return $syncFileOutput | ConvertFrom-Json -ErrorAction Stop
        } finally { $syncFileProcess.Dispose() }
    }
    Assert-SyncTest ($syncBlankProfile.TaskName -ceq 'CodexHistorySync-Public') 'default name avoids private task'
    Assert-SyncTest ($syncProfile.RuntimeDirectory -ieq (Join-Path $syncProfileDirectory 'local runtime')) 'relative runtime uses config directory'
    Assert-SyncTest ((Resolve-SyncAbsolutePath '~') -ieq [Environment]::GetFolderPath('UserProfile')) 'tilde root'
    Assert-SyncTest ((Resolve-SyncPython $syncProfile $syncPython) -ieq $syncPython) 'explicit interpreter override'
    $syncExpectedChinese = -join @([char]0x72b6,[char]0x6001,[char]0x6b63,[char]0x5e38)
    foreach ($syncWrapper in @('sync-now.ps1','status.ps1','doctor.ps1')) {
        $syncText = & (Join-Path $syncScriptDirectory $syncWrapper) -ConfigPath $syncTestConfigPath -PythonPath $syncPython
        Assert-SyncTest ($syncText -ceq $syncExpectedChinese) "$syncWrapper native UTF8 output"
        $syncResult = & (Join-Path $syncScriptDirectory $syncWrapper) -ConfigPath $syncTestConfigPath -PythonPath $syncPython -Json | ConvertFrom-Json
        $syncExpectedMode = @{'sync-now.ps1'='--once';'status.ps1'='--status';'doctor.ps1'='--doctor'}[$syncWrapper]
        Assert-SyncTest (@($syncResult.argv) -contains $syncExpectedMode) "$syncWrapper mode forwarding"
        Assert-SyncTest (@($syncResult.argv) -contains $syncProfile.RuntimeDirectory) "$syncWrapper runtime forwarding"
        Assert-SyncTest (@($syncResult.argv) -contains '--python-windows') "$syncWrapper endpoint interpreter forwarding"
        Assert-SyncTest (@($syncResult.argv) -contains '--json') "$syncWrapper JSON forwarding"
    }
    # Native -File parameter binding differs from invoking a script in this session.
    # All endpoint Python files in these child processes are harmless local stubs.
    foreach ($syncWrapper in @('status.ps1', 'doctor.ps1', 'configure.ps1')) {
        $syncFileExtra = @('-Json')
        if ($syncWrapper -ceq 'configure.ps1') { $syncFileExtra = @('-NonInteractive') }
        $syncFileResult = Invoke-SyncTestFile $syncWrapper $syncFileExtra
        $syncDefaultConfigPath = Join-Path $syncScriptDirectory 'config.json'
        Assert-SyncTest (@($syncFileResult.argv) -contains $syncDefaultConfigPath) "$syncWrapper native -File uses script-local default config"
        Assert-SyncTest (@($syncFileResult.argv) -contains '--python-windows') "$syncWrapper native -File forwards interpreter"
        Assert-SyncTest (-not (Test-Path -LiteralPath $syncDefaultConfigPath)) "$syncWrapper native -File missing config stays read-only with stub"
    }
    Assert-SyncTest (-not (Test-Path -LiteralPath (Join-Path $syncScriptDirectory 'runtime'))) 'default native -File preflights create no runtime'
    $syncFileResult = Invoke-SyncTestFile 'status.ps1' @('-Json') $syncProfileDirectory
    Assert-SyncTest (@($syncFileResult.argv) -contains (Join-Path $syncScriptDirectory 'config.json')) 'absolute -File from another working directory still uses script-local default config'
    foreach ($syncMode in @('DryRun','Initialize')) {
        $syncModeParameters=@{ConfigPath=$syncTestConfigPath;PythonPath=$syncPython;Json=$true}
        $syncModeParameters[$syncMode]=$true
        $syncResult=& (Join-Path $syncScriptDirectory 'sync-now.ps1') @syncModeParameters | ConvertFrom-Json
        $syncExpected=@{DryRun='--dry-run';Initialize='--initialize'}[$syncMode]
        Assert-SyncTest (@($syncResult.argv) -contains $syncExpected) "$syncMode forwarding"
    }
    Assert-SyncReject { & (Join-Path $syncScriptDirectory 'sync-now.ps1') -ConfigPath $syncTestConfigPath -DryRun -Initialize } 'cannot be used together' 'mutually exclusive modes'
    $syncResult=& (Join-Path $syncScriptDirectory 'configure.ps1') -ConfigPath $syncTestConfigPath -PythonPath $syncPython -Force -NonInteractive -Distro 'Sample Distro' -WslUser 'your-linux-user' -RuntimeDirectory 'state folder' -TaskName 'Public-Profile' -RunBudgetSeconds '120' | ConvertFrom-Json
    foreach ($syncExpected in @('--force','--non-interactive','Sample Distro','your-linux-user','state folder','Public-Profile','--run-budget-seconds','120')) {
        Assert-SyncTest (@($syncResult.argv) -contains $syncExpected) "configure forwarding $syncExpected"
    }
    $syncBad=[pscustomobject]@{python_windows='missing-executable.exe'}
    $syncBadProfile=[pscustomobject]@{Configuration=$syncBad;ConfigDirectory=$syncProfileDirectory}
    Assert-SyncReject { Resolve-SyncPython $syncBadProfile } 'does not exist' 'no silent fallback for broken configured path'
    Assert-SyncTest ((Resolve-SyncPython $syncBadProfile $syncPython) -ieq $syncPython) 'explicit override wins over invalid config'
    $syncBad.python_windows='auto'
    $syncPriorPath = [Environment]::GetEnvironmentVariable('PATH', 'Process')
    try {
        [Environment]::SetEnvironmentVariable('PATH', (Split-Path $syncPython -Parent) + ';' + $syncPriorPath, 'Process')
        Assert-SyncTest ((Resolve-SyncPython $syncBadProfile) -ieq $syncPython) 'auto discovers CPython from PATH, including setup-python installations'
    } finally { [Environment]::SetEnvironmentVariable('PATH', $syncPriorPath, 'Process') }

    # Direct ownership checks do not read or mutate the real scheduler.
    $syncIdentity=Get-SyncTaskIdentity $syncProfile $syncPython $syncScriptDirectory
    $syncFake=[pscustomobject]@{Description=$syncIdentity.Description;Actions=@([pscustomobject]@{Execute=$syncPythonw;Arguments=$syncIdentity.Arguments;WorkingDirectory=$syncScriptDirectory})}
    Assert-SyncTaskOwnership $syncFake $syncProfile $syncScriptDirectory
    $syncPassed++
    $syncOtherProfile=Get-SyncProfile (Join-Path $syncProfileDirectory 'second.json') -AllowMissingConfiguration
    Assert-SyncReject { Assert-SyncTaskOwnership $syncFake $syncOtherProfile $syncScriptDirectory } 'another directory, configuration or runtime' 'other profile ownership rejection'
    $syncFake.Description='CodexHistorySync v1; directory=private-existing-deployment'
    Assert-SyncReject { Assert-SyncTaskOwnership $syncFake $syncProfile $syncScriptDirectory } 'another directory, configuration or runtime' 'private v1 task rejection'
    $syncFake.Description=$syncIdentity.Description
    $syncOldPython=Join-Path $syncTestRoot 'retired-python\python.exe'
    $syncOldIdentity=Get-SyncTaskIdentity $syncProfile $syncOldPython $syncScriptDirectory
    $syncFake.Actions[0].Arguments=$syncOldIdentity.Arguments
    $syncFake.Actions[0].Execute=Join-Path (Split-Path $syncOldPython -Parent) 'pythonw.exe'
    Assert-SyncTaskOwnership $syncFake $syncProfile $syncScriptDirectory
    $syncPassed++

    # Task Scheduler restrictions also include ASCII DEL, empty names and non-strings.
    $syncInvalidTaskCharacters = @([char]'\', [char]'/', [char]':', [char]'*', [char]'?', [char]'"', [char]'<', [char]'>', [char]'|')
    $syncInvalidTaskCharacters += @(0..31 | ForEach-Object { [char]$_ })
    $syncInvalidTaskCharacters += [char]127
    foreach ($syncInvalidCharacter in $syncInvalidTaskCharacters) {
        $syncInvalidNameConfig = $syncValid | ConvertTo-Json -Compress | ConvertFrom-Json
        $syncInvalidNameConfig.task_name = 'invalid' + $syncInvalidCharacter + 'name'
        Write-SyncTestJson $syncTestConfigPath $syncInvalidNameConfig
        Assert-SyncReject { Get-SyncProfile $syncTestConfigPath } 'without reserved characters or ASCII controls' ("task name forbids character " + [int]$syncInvalidCharacter)
    }
    foreach ($syncInvalidName in @('', '   ', $null, $true, 123)) {
        $syncInvalidNameConfig = $syncValid | ConvertTo-Json -Compress | ConvertFrom-Json
        $syncInvalidNameConfig.task_name = $syncInvalidName
        Write-SyncTestJson $syncTestConfigPath $syncInvalidNameConfig
        Assert-SyncReject { Get-SyncProfile $syncTestConfigPath } 'non-empty string' 'task name requires a non-empty string'
    }
    $syncInvalidNameConfig = $syncValid | ConvertTo-Json -Compress | ConvertFrom-Json
    $syncInvalidNameConfig.task_name = 'a' * 201
    Write-SyncTestJson $syncTestConfigPath $syncInvalidNameConfig
    Assert-SyncReject { Get-SyncProfile $syncTestConfigPath } 'at most 200' 'task name length limit'
    $syncInvalidNameConfig.task_name = 'a' * 200
    Write-SyncTestJson $syncTestConfigPath $syncInvalidNameConfig
    Assert-SyncTest ((Get-SyncProfile $syncTestConfigPath).TaskName.Length -eq 200) '200-character task name accepted'
    Write-SyncTestJson $syncTestConfigPath $syncValid

    # Scheduler lookup and mutations are mocked; constructors only build local definitions.
    & (Join-Path $syncScriptDirectory 'install.ps1') -ConfigPath $syncTestConfigPath -PythonPath $syncPython -WhatIf
    Assert-SyncTest $? 'valid isolated WhatIf'
    foreach ($syncKey in @('quiet_seconds','operation_timeout_seconds','max_transfers_per_run','run_budget_seconds','interval_minutes','task_time_limit_minutes')) {
        foreach ($syncRaw in @('true','1.0','0','-1','null','"120"','[]')) {
            $syncRawConfig=($syncValid | ConvertTo-Json -Compress)
            $syncRawConfig=$syncRawConfig.Substring(0,$syncRawConfig.Length-1)+',"'+$syncKey+'":'+$syncRaw+'}'
            [System.IO.File]::WriteAllText($syncTestConfigPath,$syncRawConfig,$syncUtf8)
            Assert-SyncReject { & (Join-Path $syncScriptDirectory 'install.ps1') -ConfigPath $syncTestConfigPath -PythonPath $syncPython -WhatIf } 'positive integer' "$syncKey=$syncRaw"
        }
    }
    $syncShortConfig=$syncValid | ConvertTo-Json -Compress | ConvertFrom-Json
    $syncShortConfig | Add-Member -NotePropertyName task_time_limit_minutes -NotePropertyValue 8
    Write-SyncTestJson $syncTestConfigPath $syncShortConfig
    Assert-SyncReject { & (Join-Path $syncScriptDirectory 'install.ps1') -ConfigPath $syncTestConfigPath -PythonPath $syncPython -WhatIf } 'must allow run_budget_seconds' 'hard limit protects transfer completion'
    Write-SyncTestJson $syncTestConfigPath $syncValid
    foreach ($syncBadLedger in @('{"version":true,"threads":{}}','{"version":2,"threads":{}}','{"version":1,"threads":[]}','{"version":1}')) {
        [System.IO.File]::WriteAllText($syncLedgerPath,$syncBadLedger,$syncUtf8)
        Assert-SyncReject { & (Join-Path $syncScriptDirectory 'install.ps1') -ConfigPath $syncTestConfigPath -PythonPath $syncPython -WhatIf } 'numeric version 1' 'invalid ledger rejection'
    }
    [System.IO.File]::WriteAllText($syncLedgerPath,'{"version":1,"threads":{}}',$syncUtf8)
    [System.IO.File]::WriteAllText((Join-Path $syncProfile.RuntimeDirectory 'pending.json'),'{}',$syncUtf8)
    Assert-SyncReject { & (Join-Path $syncScriptDirectory 'install.ps1') -ConfigPath $syncTestConfigPath -PythonPath $syncPython -WhatIf } 'unfinished import' 'pending import blocks registration'
    & (Join-Path $syncScriptDirectory 'uninstall.ps1') -ConfigPath $syncTestConfigPath -WhatIf
    Assert-SyncTest $? 'uninstall safely skips absent unique test task'
    Remove-Item -LiteralPath (Join-Path $syncProfile.RuntimeDirectory 'pending.json') -Force
    $syncOwnedTask = [pscustomobject]@{
        TaskName=$syncProfile.TaskName; Description=$syncIdentity.Description
        Actions=@([pscustomobject]@{Execute=$syncPythonw; Arguments=$syncIdentity.Arguments; WorkingDirectory=$syncScriptDirectory})
    }
    $syncMockState.Tasks = @($syncOwnedTask)
    & (Join-Path $syncScriptDirectory 'install.ps1') -ConfigPath $syncTestConfigPath -PythonPath $syncPython -WhatIf
    Assert-SyncTest $? 'owned task update passes WhatIf'
    & (Join-Path $syncScriptDirectory 'uninstall.ps1') -ConfigPath $syncTestConfigPath -WhatIf
    Assert-SyncTest $? 'owned task uninstall passes WhatIf'
    Assert-SyncTest (Test-Path -LiteralPath $syncLedgerPath -PathType Leaf) 'uninstall preserves baseline'
    $syncOwnedTask.Description='unrelated application task'
    Assert-SyncReject { & (Join-Path $syncScriptDirectory 'install.ps1') -ConfigPath $syncTestConfigPath -PythonPath $syncPython -WhatIf } 'another directory, configuration or runtime' 'installer refuses unrelated same-name task'
    Assert-SyncReject { & (Join-Path $syncScriptDirectory 'uninstall.ps1') -ConfigPath $syncTestConfigPath -WhatIf } 'another directory, configuration or runtime' 'uninstaller refuses unrelated same-name task'
    Assert-SyncTest ($syncMockState.GetCalls -gt 0) 'scheduler lookups reached the mock'
    Assert-SyncTest ($syncMockState.RegisterCalls -eq 0) 'no scheduler registration attempted'
    Assert-SyncTest ($syncMockState.UnregisterCalls -eq 0) 'no scheduler removal attempted'
} finally {
    # Resolve and re-check the one owned TEMP directory immediately before recursive removal.
    if (Test-Path -LiteralPath $syncOwnedTestRoot) {
        $syncCleanupRoot = [System.IO.Path]::GetFullPath((Resolve-Path -LiteralPath $syncOwnedTestRoot).ProviderPath)
        $syncTempBoundary = $syncTempDirectory + [System.IO.Path]::DirectorySeparatorChar
        $syncCleanupLeaf = [System.IO.Path]::GetFileName($syncCleanupRoot)
        $syncCleanupItem = Get-Item -LiteralPath $syncCleanupRoot -Force
        if (-not $syncCleanupRoot.Equals($syncOwnedTestRoot, [StringComparison]::OrdinalIgnoreCase) -or
            -not $syncCleanupRoot.StartsWith($syncTempBoundary, [StringComparison]::OrdinalIgnoreCase) -or
            $syncCleanupLeaf -cne $syncTestLeaf -or
            $syncCleanupLeaf -cnotmatch '^codex-history-sync-public-tests-[a-f0-9]{32}$' -or
            ($syncCleanupItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)) {
            throw 'Refusing recursive cleanup: target is not the owned test directory within system TEMP.'
        }
        Remove-Item -LiteralPath $syncCleanupRoot -Recurse -Force
        if (Test-Path -LiteralPath $syncCleanupRoot) { throw 'The isolated test directory was not fully cleaned.' }
    }
    $syncCleaned = $true
}
Write-Output "PASS $syncPassed isolated public PowerShell tests on $($PSVersionTable.PSVersion). No real scheduler queries, task changes or WSL starts."
Write-Output "Temporary fixtures cleaned: $syncCleaned"
