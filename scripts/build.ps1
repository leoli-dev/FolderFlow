[CmdletBinding()]
param(
    [string]$PythonPath = "python",
    [switch]$SkipTests,
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$venvRoot = Join-Path $repoRoot ".venv"
$venvPython = Join-Path $venvRoot "Scripts\python.exe"
$buildRoot = Join-Path $repoRoot "build"
$distRoot = Join-Path $repoRoot "dist"
$previousPythonPath = $env:PYTHONPATH
$previousExecutablePath = $env:PATH
$previousLocation = Get-Location

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

try {
    Set-Location -LiteralPath $repoRoot
    Invoke-Checked $PythonPath @("-c", "import sys,struct; assert sys.platform=='win32', 'Build on Windows'; assert sys.version_info[:2]==(3,13), 'Use Python 3.13'; assert struct.calcsize('P')==8, 'Use 64-bit Python'")
    if (-not (Test-Path -LiteralPath $venvPython)) {
        Invoke-Checked $PythonPath @("-m", "venv", $venvRoot)
    }
    Invoke-Checked $venvPython @("-c", "import sys,struct; assert sys.version_info[:2]==(3,13) and struct.calcsize('P')==8, 'Recreate .venv with Python 3.13 x64'")
    $pythonBase = (& $venvPython -c "import sys; print(sys.base_prefix)").Trim()
    $env:PATH = ((Join-Path $venvRoot "Scripts"), $pythonBase, (Join-Path $env:SystemRoot "System32"), $env:SystemRoot) -join ";"
    if (-not $SkipDependencyInstall) {
        Invoke-Checked $venvPython @("-m", "pip", "install", "--disable-pip-version-check", "-r", (Join-Path $repoRoot "requirements-build.txt"))
    }
    $env:PYTHONPATH = Join-Path $repoRoot "src"
    if (-not $SkipTests) { & (Join-Path $PSScriptRoot "test.ps1") -PythonPath $venvPython }
    New-Item -ItemType Directory -Path $buildRoot, $distRoot -Force | Out-Null
    Invoke-Checked $venvPython @((Join-Path $PSScriptRoot "write_version_info.py"), (Join-Path $buildRoot "windows-version.txt"))
    Invoke-Checked $venvPython @(
        "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--contents-directory", "_internal", "--windowed",
        "--name", "FolderFlow", "--paths", (Join-Path $repoRoot "src"),
        "--distpath", (Join-Path $buildRoot "binary"),
        "--workpath", (Join-Path $buildRoot "pyinstaller"),
        "--specpath", $buildRoot,
        "--manifest", (Join-Path $repoRoot "packaging\windows.manifest"),
        "--version-file", (Join-Path $buildRoot "windows-version.txt"),
        (Join-Path $repoRoot "src\launcher.py")
    )
    Invoke-Checked $venvPython @((Join-Path $PSScriptRoot "package_release.py"))
    Write-Host "Build complete. Portable application and ZIP: $distRoot"
} finally {
    $env:PYTHONPATH = $previousPythonPath
    $env:PATH = $previousExecutablePath
    Set-Location -LiteralPath $previousLocation.Path
}
