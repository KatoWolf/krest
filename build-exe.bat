@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "KREST_BUILD_PY=py -3.12"
py -3.12 --version >nul 2>&1
if errorlevel 1 set "KREST_BUILD_PY=python"
%KREST_BUILD_PY% -c "import sys; assert sys.version_info[:2] in ((3,11),(3,12)) and sys.maxsize > 2**32" >nul 2>&1
if errorlevel 1 goto python_missing
if not exist .build-venv\Scripts\python.exe (
    %KREST_BUILD_PY% -m venv .build-venv
    if errorlevel 1 goto failed
)
echo Preparing the EXE builder. First run downloads dependencies.
.build-venv\Scripts\python.exe -m pip install "torch>=2.2,<3" --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto failed
.build-venv\Scripts\python.exe -m pip install -r requirements-windows.txt -r requirements-voice.txt -r requirements-build.txt
if errorlevel 1 goto failed
.build-venv\Scripts\python.exe packaging\build_windows.py %*
if errorlevel 1 goto failed
echo Done. Distribute the whole dist\Krest folder or the ZIP in dist.
pause
exit /b 0
:python_missing
echo Python 3.11 or 3.12 x64 is needed on the author's computer.
echo Run start.bat to prepare Python, then run this builder again.
pause
exit /b 1
:failed
echo Build failed. No new release has been published. See the error above.
pause
exit /b 1
