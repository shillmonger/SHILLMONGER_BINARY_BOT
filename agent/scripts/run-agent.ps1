# SHILLMONGER Windows Agent — local run (foreground, Ctrl+C to stop).
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot\..
if (-not (Test-Path .venv\Scripts\python.exe)) {
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}
.\.venv\Scripts\python.exe -m agent
