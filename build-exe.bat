@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call packaging\find_python.bat
if errorlevel 1 goto python_missing
"%KREST_PYTHON%" %KREST_PY_ARGS% packaging\build_bootstrap.py %*
if errorlevel 1 goto failed
echo Done. Distribute the whole dist\Krest folder or the ZIP in dist.
if defined CI exit /b 0
pause
exit /b 0
:python_missing
echo Python 3.11 or 3.12 x64 is needed on the author's computer.
echo Run start.bat to prepare Python, then run this builder again.
if defined CI exit /b 1
pause
exit /b 1
:failed
echo Build failed. Full details: build\build-last.log
if defined CI exit /b 1
pause
exit /b 1
