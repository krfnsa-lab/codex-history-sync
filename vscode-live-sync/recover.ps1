param([Parameter(Mandatory=$true)][Alias('ConfigPath')][string]$Config, [string]$Controller)
$ErrorActionPreference = 'Stop'
. (Join-Path (Split-Path $PSScriptRoot -Parent) 'Common.ps1')
$profile = Get-SyncProfile -ConfigPath $Config
$configPath = $profile.ConfigPath
$arguments = @((Join-Path $PSScriptRoot 'recover.py'), '--config', $configPath)
if ($Controller) { $arguments += @('--controller', (Resolve-Path -LiteralPath $Controller).Path) }
$pythonPath = Resolve-SyncPython -Profile $profile
Invoke-SyncPython -PythonPath $pythonPath -ArgumentList $arguments
if ($LASTEXITCODE -ne 0) { throw '恢复未通过检查，暂停保护已保留。' }
