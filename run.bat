@echo off
setlocal
title gclassfetch

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed or not in your system PATH.
    echo Please install Python 3.8+ from https://www.python.org/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Starting gclassfetch...
python -u app.py %*
if errorlevel 1 (
    echo.
    echo App closed with an error code. Check the message above.
    pause
)
