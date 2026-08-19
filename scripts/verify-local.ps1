$ErrorActionPreference = 'Stop'
$env:GROWTH_OS_ENV = 'local'
$env:DATABASE_ENGINE = 'sqlite'

$python = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Local virtual environment is missing. Create .venv and install requirements.txt first.'
}

function Invoke-CheckedPython {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)
    & $python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
    }
}

$manage = Join-Path $PSScriptRoot '..\manage.py'
Invoke-CheckedPython $manage check
Invoke-CheckedPython $manage makemigrations --check --dry-run
Invoke-CheckedPython $manage migrate --noinput
Invoke-CheckedPython $manage test
