@echo off
setlocal

set "CONDA_EXE=C:\ProgramData\miniforge3\Scripts\conda.exe"
if not exist "%CONDA_EXE%" (
    where conda >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] conda executable not found
        pause
        exit /b 1
    )
    set "CONDA_EXE=conda"
)

cd /d d:\Uni-Lab-OS\unilabos\devices

"%CONDA_EXE%" run --no-capture-output -n base python -m eit_agv.controller.auto_charge_pp5_cp6_monitor %*
if errorlevel 1 (
    set "EXIT_CODE=%ERRORLEVEL%"
    echo [ERROR] monitor exited with code %EXIT_CODE%
    pause
    exit /b %EXIT_CODE%
)

if not "%1"=="" goto :eof

echo [INFO] monitor exited normally
pause
goto :eof
