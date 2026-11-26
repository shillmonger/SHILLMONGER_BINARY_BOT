# Remove the Task Scheduler autostart entry and stop a running instance.
$ErrorActionPreference = 'Stop'
$TaskName = 'SHILLMONGER-Agent'

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Get-Process python,pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -like '*agent*' -or $_.CommandLine -like '*agent*' } |
    Stop-Process -Force -ErrorAction SilentlyContinue

Write-Host "Autostart removed. To fully stop a foreground run, press Ctrl+C in its window."
