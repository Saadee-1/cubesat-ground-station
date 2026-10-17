@echo off
setlocal
cd /d "%~dp0"
rem Finds a Python 3.10 or newer: "python" first, then the "py -3" launcher.
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY goto nopython
%PY% -c "import pytest" >nul 2>&1
if errorlevel 1 goto nopytest
%PY% -m pytest %*
exit /b %errorlevel%
:nopytest
echo pytest is not installed for %PY%. Install it with:  %PY% -m pip install pytest
exit /b 2
:nopython
echo Python 3.10 or newer was not found. Install it from python.org and tick "Add python.exe to PATH",
echo or install the Windows py launcher. Then run this file again.
exit /b 2
