@echo off
cd /d "%~dp0"
echo This author tool needs Python 3.12 x64, Visual Studio C++ Build Tools and Inno Setup 6.
py -3.12 -m venv .build-venv
if errorlevel 1 goto failed
.build-venv\Scripts\python.exe -m pip install "torch>=2.2,<3" --index-url https://download.pytorch.org/whl/cpu
if errorlevel 1 goto failed
.build-venv\Scripts\python.exe -m pip install -r requirements-windows.txt -r requirements-voice.txt -r requirements-build.txt
if errorlevel 1 goto failed
.build-venv\Scripts\python.exe packaging\build_windows.py
if errorlevel 1 goto failed
echo Installer is in the dist folder.
pause
exit /b 0
:failed
echo Build failed. See the error above. No installer was published.
pause
exit /b 1
