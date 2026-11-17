$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    $python = Join-Path $root ".venv312\Scripts\python.exe"
    if (-not (Test-Path $python)) {
        $python = "python"
    }
    & $python -m evaluation.cli all
    if ($LASTEXITCODE -ne 0) { throw "evaluation command failed with exit code $LASTEXITCODE" }
    & $python -m pytest evaluation/tests -q
    if ($LASTEXITCODE -ne 0) { throw "evaluation tests failed with exit code $LASTEXITCODE" }
    Write-Host "AG-07 evaluation and tests completed."
}
finally {
    Pop-Location
}
