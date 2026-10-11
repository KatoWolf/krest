@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call packaging\find_python.bat
if errorlevel 1 goto python_missing
if not exist .build-venv\Scripts\python.exe (
    "%KREST_PYTHON%" %KREST_PY_ARGS% -m venv .build-venv
    if errorlevel 1 goto failed
)
.build-venv\Scripts\python.exe -c "import sys; assert sys.version_info[:2] in ((3,11),(3,12)) and sys.maxsize > 2**32" >nul 2>&1
if errorlevel 1 goto invalid_cache
echo Preparing the EXE builder. First run downloads dependencies.
echo Vosk and Silero models are included automatically. The voice is tested before creating the ZIP.
.build-venv\Scripts\python.exe -m pip install "torch>=2.2,<3" --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto failed
.build-venv\Scripts\python.exe -m pip install -r requirements-windows.txt -r requirements-voice.txt -r requirements-build.txt
if errorlevel 1 goto failed
.build-venv\Scripts\python.exe packaging\build_windows.py %*
if errorlevel 1 goto failed
echo Done. Distribute the whole dist\Krest folder or the ZIP in dist.
pause
exit /b 0
:invalid_cache
echo The builder cache uses an unsupported Python. Remove only .build-venv and run this file again.
pause
exit /b 1
:python_missing
echo Python 3.11 or 3.12 x64 is needed on the author's computer.
echo Run start.bat to prepare Python, then run this builder again.
pause
exit /b 1
:failed
echo Build failed. No new release has been published. See the error above.
pause
exit /b 1
