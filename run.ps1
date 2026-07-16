$ErrorActionPreference = "Stop"

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "The .venv environment was not found."
}

& $python (Join-Path $PSScriptRoot "main.py")
