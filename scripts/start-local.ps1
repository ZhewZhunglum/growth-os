$ErrorActionPreference = 'Stop'
$env:GROWTH_OS_ENV = 'local'
$env:DATABASE_ENGINE = 'sqlite'

$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Local virtual environment is missing. Create .venv and install requirements.txt first.'
}

& $python (Join-Path $PSScriptRoot '..\manage.py') runserver 127.0.0.1:8000

