param(
    [Parameter(Mandatory=$true)][Alias('ConfigPath')][string]$Config,
    [string]$Controller,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'Common.ps1')
$profile = Get-SyncProfile -ConfigPath $Config
$configPath = $profile.ConfigPath
$arguments = @((Join-Path $PSScriptRoot 'install.py'), '--config', $configPath)
if ($Controller) { $arguments += @('--controller', (Resolve-Path -LiteralPath $Controller).Path) }
if ($WhatIf) { $arguments += '--dry-run' }
$pythonPath = Resolve-SyncPython -Profile $profile
Invoke-SyncPython -PythonPath $pythonPath -ArgumentList $arguments
if ($LASTEXITCODE -ne 0) { throw '安装未完成，请保留现有同步基线并检查错误。' }
