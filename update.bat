@echo off
rem Start a separate script so updating this batch file cannot corrupt its execution.
start "Krest update" powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\update_source.ps1"
exit /b
