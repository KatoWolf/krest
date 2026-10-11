$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$root = Split-Path $PSScriptRoot -Parent
Set-Location -LiteralPath $root
try {
    if (-not (Test-Path -LiteralPath (Join-Path $root '.git'))) {
        throw 'This is a source ZIP, not a Git clone. Download main.zip from github.com/KatoWolf/krest, extract separately and keep your model folder and personal files. EXE users need the latest release ZIP.'
    }
    if (-not (Get-Command git.exe -ErrorAction SilentlyContinue)) {
        throw 'Install Git for Windows from https://git-scm.com/download/win and run update.bat again.'
    }
    $candidates = @((Join-Path $root '.venv\Scripts\python.exe'), (Join-Path $root '.krest-runtime\Scripts\python.exe'), (Join-Path $env:LOCALAPPDATA 'Krest\Python312\python.exe'))
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        foreach ($version in @('-3.12', '-3.11')) {
            try {
                $found = & $launcher.Source $version -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0 -and $found) { $candidates += [string]$found }
            } catch { }
        }
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notmatch 'WindowsApps') { $candidates += $command.Source }
    $python = $null
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            try {
                & $candidate -c "import sys, struct, tkinter; assert sys.version_info[:2] in ((3,11),(3,12)) and struct.calcsize('P') == 8" 2>$null
                if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
            } catch { }
        }
    }
    if (-not $python) { throw 'Run start.bat to prepare Python, then exit Krest and run update.bat again.' }
    Write-Host 'Updating Krest from origin/main. Local source changes will stop the update.'
    Write-Host 'Exit Krest completely, including its tray icon, before updating.'
    & $python (Join-Path $root 'main.py') --update
    if ($LASTEXITCODE -ne 0) { throw 'Update stopped. See the reason above. Local edits were not discarded.' }
    Write-Host 'Opening the launcher to check dependencies for the new version.'
    Start-Process -FilePath (Join-Path $root 'start.bat') -WorkingDirectory $root
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Yellow
    Read-Host 'Press Enter to close'
    exit 1
}
