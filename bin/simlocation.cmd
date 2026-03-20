@echo off
setlocal

:: SimLocation Windows launcher
:: Finds a suitable Python and launches simlocation.py

set "SCRIPT_DIR=%~dp0"
set "PYTHON_IMPL=%SCRIPT_DIR%simlocation.py"

if defined SIMLOCATION_PYTHON (
    if exist "%SIMLOCATION_PYTHON%" (
        set "PYTHON_BIN=%SIMLOCATION_PYTHON%"
        goto :run
    )
    echo SIMLOCATION_PYTHON is set but not found: %SIMLOCATION_PYTHON% >&2
    exit /b 1
)

:: Try python3 first, then python
where python3 >nul 2>&1
if %errorlevel% equ 0 (
    python3 -c "import requests, pymobiledevice3" >nul 2>&1
    if %errorlevel% equ 0 (
        set "PYTHON_BIN=python3"
        goto :run
    )
)

where python >nul 2>&1
if %errorlevel% equ 0 (
    python -c "import requests, pymobiledevice3" >nul 2>&1
    if %errorlevel% equ 0 (
        set "PYTHON_BIN=python"
        goto :run
    )
)

echo Unable to find a Python runtime for simlocation. >&2
echo Set SIMLOCATION_PYTHON to a Python with requests and pymobiledevice3. >&2
exit /b 1

:run
"%PYTHON_BIN%" "%PYTHON_IMPL%" %*
