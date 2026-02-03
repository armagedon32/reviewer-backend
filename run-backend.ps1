Set-StrictMode -Version Latest

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8000
