@echo off
setlocal enabledelayedexpansion

echo ================================================
echo Uni-Lab-OS Environment Installation Script
echo ================================================
echo.

REM Get the directory where this script is located
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

REM Find conda installation
echo Searching for conda installation...

REM Method 1: Try to get conda base using 'conda info --base'
set "CONDA_BASE="
for /f "tokens=*" %%i in ('conda info --base 2^>nul') do (
    set "CONDA_BASE=%%i"
)

if not "%CONDA_BASE%"=="" (
    echo Found conda at: %CONDA_BASE% (via conda info)
    goto :conda_found
)

REM Method 2: Use 'where conda' and parse the path
echo Trying alternative method...
for /f "tokens=*" %%i in ('where conda 2^>nul') do (
    set "CONDA_PATH=%%i"
    goto :parse_conda_path
)

echo ERROR: Could not find conda installation!
echo Please make sure conda/mamba is installed and in your PATH.
echo.
pause
exit /b 1

:parse_conda_path
REM Parse conda path to find base directory
REM Common paths:
REM   C:\Users\hp\miniforge3\Library\bin\conda.bat
REM   C:\Users\hp\miniforge3\Scripts\conda.exe
REM   C:\Users\hp\miniforge3\condabin\conda.bat

echo Found conda executable at: %CONDA_PATH%

REM Check if path contains \Library\bin\ (typical for conda.bat)
echo %CONDA_PATH% | findstr /C:"\Library\bin\" >nul
if not errorlevel 1 (
    REM Path like: C:\Users\hp\miniforge3\Library\bin\conda.bat
    REM Need to go up 3 levels: bin -> Library -> miniforge3
    for %%i in ("%CONDA_PATH%") do set "CONDA_BASE=%%~dpi"
    for %%i in ("%CONDA_BASE%..\..\..") do set "CONDA_BASE=%%~fi"
    goto :conda_found
)

REM Check if path contains \Scripts\ (typical for conda.exe)
echo %CONDA_PATH% | findstr /C:"\Scripts\" >nul
if not errorlevel 1 (
    REM Path like: C:\Users\hp\miniforge3\Scripts\conda.exe
    REM Need to go up 2 levels: Scripts -> miniforge3
    for %%i in ("%CONDA_PATH%") do set "CONDA_BASE=%%~dpi"
    for %%i in ("%CONDA_BASE%..\.") do set "CONDA_BASE=%%~fi"
    goto :conda_found
)

REM Check if path contains \condabin\ (typical for conda.bat)
echo %CONDA_PATH% | findstr /C:"\condabin\" >nul
if not errorlevel 1 (
    REM Path like: C:\Users\hp\miniforge3\condabin\conda.bat
    REM Need to go up 2 levels: condabin -> miniforge3
    for %%i in ("%CONDA_PATH%") do set "CONDA_BASE=%%~dpi"
    for %%i in ("%CONDA_BASE%..\.") do set "CONDA_BASE=%%~fi"
    goto :conda_found
)

REM Default: assume it's 2 levels up
for %%i in ("%CONDA_PATH%") do set "CONDA_BASE=%%~dpi"
for %%i in ("%CONDA_BASE%..\.") do set "CONDA_BASE=%%~fi"

:conda_found
echo Found conda base directory: %CONDA_BASE%
echo.

REM Set target environment path
set "ENV_NAME=unilab"
if not "%~1"=="" set "ENV_NAME=%~1"
set "ENV_PATH=%CONDA_BASE%\envs\%ENV_NAME%"

REM Check if environment already exists
if exist "%ENV_PATH%" (
    echo ERROR: Environment already exists at %ENV_PATH%. Nothing was removed.
    echo Use a new name: install_unilab.bat unilab-hostlink
    exit /b 1
)

REM Find the packed environment file
set "PACK_FILE="
for %%f in (unilab-env*.tar.gz) do (
    set "PACK_FILE=%%f"
    goto :found_pack
)

:found_pack
if "%PACK_FILE%"=="" (
    echo ERROR: Could not find unilab-env*.tar.gz file!
    echo Please make sure the packed environment file is in the same directory as this script.
    echo.
    pause
    exit /b 1
)

echo Found packed environment: %PACK_FILE%
echo.

REM Extract the packed environment
echo Extracting environment to %ENV_PATH%...
mkdir "%ENV_PATH%"

REM Extract using tar (available in Windows 10+)
tar -xzf "%PACK_FILE%" -C "%ENV_PATH%"
if errorlevel 1 (
    echo ERROR: Failed to extract environment!
    echo Make sure you have Windows 10 or later with tar support.
    pause
    exit /b 1
)

echo.
echo Unpacking conda environment...
echo Changing to environment directory: %ENV_PATH%
cd /d "%ENV_PATH%"

REM Run conda-unpack from the environment directory
if exist "Scripts\conda-unpack.exe" (
    echo Running: .\Scripts\conda-unpack.exe
    .\Scripts\conda-unpack.exe
) else if exist "Scripts\activate.bat" (
    echo Running: .\Scripts\activate.bat followed by conda-unpack
    call .\Scripts\activate.bat
    conda-unpack
) else (
    echo ERROR: Could not find Scripts\conda-unpack.exe or Scripts\activate.bat!
    echo Current directory: %CD%
    echo Expected location: %ENV_PATH%\Scripts\
    pause
    exit /b 1
)

if errorlevel 1 (
    echo ERROR: conda-unpack failed!
    pause
    exit /b 1
)

echo.
echo Checking Uni-Lab-OS entry point...
set "UNILAB_SCRIPT=%ENV_PATH%\Scripts\unilab.exe"
if not exist "%UNILAB_SCRIPT%" (
    echo ERROR: Entry point is missing. The archive is incomplete.
    exit /b 1
) else (
    echo Found: %UNILAB_SCRIPT%
)

call "%CONDA_BASE%\condabin\conda.bat" activate "%ENV_PATH%"
if errorlevel 1 exit /b 1
"%ENV_PATH%\python.exe" "%SCRIPT_DIR%verify_installation.py" --assert-no-ros
if errorlevel 1 exit /b 1
"%UNILAB_SCRIPT%" --help
if errorlevel 1 exit /b 1

echo.
echo ================================================
echo Installation completed successfully!
echo ================================================
echo.
echo To activate the environment, run:
echo   conda activate %ENV_NAME%
echo.
echo or
echo.
echo   call %ENV_PATH%\Scripts\activate.bat
echo.
echo You can verify the installation by running:
echo   cd /d "%SCRIPT_DIR%"
echo   python verify_installation.py --assert-no-ros
echo.
pause

