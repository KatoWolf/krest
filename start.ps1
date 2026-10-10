$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$root = $PSScriptRoot
function Find-Python {
    $candidates = @((Join-Path $root '.venv\Scripts\python.exe'), (Join-Path $root '.krest-runtime\Scripts\python.exe'), (Join-Path $env:LOCALAPPDATA 'Krest\Python312\python.exe'))
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $found = $null
        try { $found = & $launcher.Source -3.12 -c 'import sys; print(sys.executable)' 2>$null } catch { $found = $null }
        if ($LASTEXITCODE -eq 0 -and $found) { $candidates += [string]$found }
    }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($command -and $command.Source -notmatch 'WindowsApps') { $candidates += $command.Source }
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            $result = $null
            try { $result = & $candidate -c "import sys, struct, tkinter; assert sys.version_info[:2] in ((3,11),(3,12)) and struct.calcsize('P')==8; print(sys.executable)" 2>$null } catch { $result = $null }
            if ($LASTEXITCODE -eq 0 -and $result) { return [string]$result }
        }
    }
    return $null
}
$window = $null
$installer = $null
try {
    $python = Find-Python
    if (-not $python) {
        $answer = [System.Windows.Forms.MessageBox]::Show('Для первого запуска Крест нужен Python. Скачать его с python.org и установить только для вашего пользователя? После этого Крест сама подготовит музыку и голос.', 'Первый запуск Крест', 'YesNo', 'Information')
        if ($answer -ne 'Yes') { exit }
        $window = New-Object System.Windows.Forms.Form
        $window.Text = 'Крест — подготовка первого запуска'
        $window.Size = New-Object System.Drawing.Size(530, 150)
        $window.StartPosition = 'CenterScreen'
        $window.ControlBox = $false
        $label = New-Object System.Windows.Forms.Label
        $label.Text = 'Скачиваю Python. Команды вводить не нужно…'
        $label.AutoSize = $true
        $label.Location = New-Object System.Drawing.Point(20, 20)
        $window.Controls.Add($label)
        $bar = New-Object System.Windows.Forms.ProgressBar
        $bar.Style = 'Marquee'
        $bar.Size = New-Object System.Drawing.Size(470, 20)
        $bar.Location = New-Object System.Drawing.Point(20, 60)
        $window.Controls.Add($bar)
        $window.Show()
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $installer = Join-Path $env:TEMP ('krest-python-' + [guid]::NewGuid().ToString() + '.exe')
        $web = New-Object System.Net.WebClient
        try {
            $task = $web.DownloadFileTaskAsync([uri]'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe', $installer)
            while (-not $task.IsCompleted) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 100 }
            $task.GetAwaiter().GetResult()
        } finally { $web.Dispose() }
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'O=Python Software Foundation(?:,|$)') {
            throw 'Не удалось подтвердить подпись Python Software Foundation. Скачанный файл не запущен.'
        }
        $label.Text = 'Устанавливаю Python для текущего пользователя…'
        $target = Join-Path $env:LOCALAPPDATA 'Krest\Python312'
        $arguments = '/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0 Include_pip=1 Include_tcltk=1 TargetDir="' + $target + '"'
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -PassThru
        while (-not $process.HasExited) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 100 }
        if ($process.ExitCode -notin @(0, 3010)) { throw ('Не удалось установить Python. Код: ' + $process.ExitCode) }
        $python = Find-Python
        if (-not $python) { throw 'Python не найден после установки.' }
        $window.Close()
        $window = $null
    }
    $pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
    if (-not (Test-Path -LiteralPath $pythonw)) { $pythonw = $python }
    Start-Process -FilePath $pythonw -WorkingDirectory $root -ArgumentList ('"' + (Join-Path $root 'setup_app.py') + '"')
} catch {
    [System.Windows.Forms.MessageBox]::Show(('Не удалось подготовить запуск. Проверьте интернет и откройте start.bat снова.' + [Environment]::NewLine + $_.Exception.Message), 'Крест', 'OK', 'Error') | Out-Null
} finally {
    if ($window) { $window.Close() }
    if ($installer -and (Test-Path -LiteralPath $installer)) { Remove-Item -LiteralPath $installer -ErrorAction SilentlyContinue }
}
