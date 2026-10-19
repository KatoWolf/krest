@echo off
rem Called from a launcher; returns a validated executable and optional py arguments.
set "KREST_PYTHON="
set "KREST_PY_ARGS="
for %%P in ("%~dp0..\.venv\Scripts\python.exe" "%~dp0..\.krest-runtime\Scripts\python.exe" "%LOCALAPPDATA%\Krest\Python312\python.exe" "%~dp0..\.build-venv\Scripts\python.exe") do (
    call :probe "%%~P"
    if not errorlevel 1 exit /b 0
)
for %%V in (3.12 3.11) do (
    py -%%V -c "import sys, struct, tkinter; assert sys.version_info[:2] in ((3,11),(3,12)) and struct.calcsize('P') == 8" >nul 2>&1
    if not errorlevel 1 (
        set "KREST_PYTHON=py"
        set "KREST_PY_ARGS=-%%V"
        exit /b 0
    )
)
call :probe python
exit /b %errorlevel%
:probe
rem Return the base interpreter: a live process inside .build-venv locks repair on Windows.
set "KREST_PROBE_FILE=%TEMP%\krest-python-%RANDOM%-%RANDOM%.txt"
"%~1" -c "import sys, struct, tkinter; assert sys.version_info[:2] in ((3,11),(3,12)) and struct.calcsize('P') == 8; print(sys._base_executable)" >"%KREST_PROBE_FILE%" 2>nul
if errorlevel 1 (
    del /q "%KREST_PROBE_FILE%" >nul 2>&1
    exit /b 1
)
set /p "KREST_PYTHON="<"%KREST_PROBE_FILE%"
del /q "%KREST_PROBE_FILE%" >nul 2>&1
"%KREST_PYTHON%" -c "import sys, struct, tkinter; assert sys.prefix == sys.base_prefix and sys.version_info[:2] in ((3,11),(3,12)) and struct.calcsize('P') == 8" >nul 2>&1
if errorlevel 1 exit /b 1
exit /b 0
