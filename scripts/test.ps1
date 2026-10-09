[CmdletBinding()]
param([string]$PythonPath = "")

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if (-not $PythonPath) {
    $candidate = Join-Path $repoRoot ".venv\Scripts\python.exe"
    $PythonPath = if (Test-Path -LiteralPath $candidate) { $candidate } else { "python" }
}
$previousPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = Join-Path $repoRoot "src"
    & $PythonPath -m unittest discover -s (Join-Path $repoRoot "tests") -v
    if ($LASTEXITCODE -ne 0) { throw "Tests failed with exit code $LASTEXITCODE" }
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
