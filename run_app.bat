@echo off
setlocal
cd /d "%~dp0"
rem Finds a Python 3.10 or newer: "python" first, then the "py -3" launcher.
set "PY="
python -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" >nul 2>&1 && set "PY=py -3"
if not defined PY goto nopython
rem If the window fails with an init.tcl or tk.tcl error, a wrong TCL_LIBRARY or TK_LIBRARY is a likely cause.
rem Nothing is cleared here, the variables are only reported.
if defined TCL_LIBRARY echo Note: TCL_LIBRARY is set to %TCL_LIBRARY%. If the window does not open, clear it: set TCL_LIBRARY=
if defined TK_LIBRARY echo Note: TK_LIBRARY is set to %TK_LIBRARY%. If the window does not open, clear it: set TK_LIBRARY=
%PY% -m groundstation %*
exit /b %errorlevel%
:nopython
echo Python 3.10 or newer was not found. Install it from python.org and tick "Add python.exe to PATH",
echo or install the Windows py launcher. Then run this file again.
exit /b 2
