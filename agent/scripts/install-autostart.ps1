# Register the agent to start at user logon via Task Scheduler.
# Runs with normal user privileges — no admin required.
# Undo with scripts\uninstall-autostart.ps1
$ErrorActionPreference = 'Stop'
$TaskName = 'SHILLMONGER-Agent'
$Script = Join-Path $PSScriptRoot 'run-agent.ps1'

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Script`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description 'SHILLMONGER Windows agent (loopback health + VPS tunnel)' `
    -Force | Out-Null

Write-Host "Registered '$TaskName'. It starts at your next logon."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Check health:       Invoke-RestMethod http://127.0.0.1:8791/health"
