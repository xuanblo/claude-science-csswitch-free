@echo off
REM ============================================================
REM  CSswitch-Win portable launcher
REM
REM  Features:
REM    - bundles runtime\python (Python 3.11 + cryptography),
REM      no system Python required
REM    - bundles runtime\claude-science\claude-science.exe,
REM      no separate Claude Science install required
REM    - config / sandbox / logs all live in D:\CSSwitch-Win-Data
REM      (override by setting CSSWITCH_HOME before launching)
REM
REM  Usage:
REM    double click this file        -> open web console
REM    "<this file>" status          -> run one command
REM    "<this file>" --nopause doctor-> no pause (for scripts)
REM
REM  Keep this file CRLF + ASCII-only (see .editorconfig): cmd.exe
REM  misparses LF line endings and non-ASCII bytes, which shows up as
REM  bogus commands such as the time.exe "input new time" prompt.
REM ============================================================
setlocal enabledelayedexpansion

rem ASCII-only comments on purpose: under chcp 65001 a goto that jumps
rem across non-ASCII text can make cmd execute those bytes.

set "ROOT=%~dp0"
set "PY=%ROOT%runtime\python\python.exe"
set "NOPAUSE=0"
set "ARG="

rem ---- strip the leading --nopause flag, collecting the rest ----
:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--nopause" (
  set "NOPAUSE=1"
) else (
  set "ARG=!ARG! %~1"
)
shift
goto :parse
:parsed

if not exist "%PY%" goto :no_python

rem ---- data dir: D:\CSSwitch-Win-Data, override with CSSWITCH_HOME ----
if not defined CSSWITCH_HOME set "CSSWITCH_HOME=D:\CSSwitch-Win-Data"
if not exist "%CSSWITCH_HOME%" mkdir "%CSSWITCH_HOME%" >nul 2>nul

rem ---- strip inherited proxy vars: they make the local proxy loop
rem      back onto itself and fail with "os error 10061" ----
set "HTTP_PROXY="
set "HTTPS_PROXY="
set "ALL_PROXY="
set "FTP_PROXY="
set "http_proxy="
set "https_proxy="
set "all_proxy="
set "ftp_proxy="
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONHOME=%ROOT%runtime\python"
set "PYTHONDONTWRITEBYTECODE=1"

rem ---- dependency guard (bundled runtime should already have it) ----
"%PY%" -c "import cryptography" >nul 2>nul
if errorlevel 1 (
  echo   [i] Installing dependency cryptography into bundled runtime ...
  "%PY%" -m pip install --disable-pip-version-check "cryptography>=41.0,<47"
  if errorlevel 1 (
    echo   [X] Install failed. Check network, or run manually:
    echo       "%PY%" -m pip install cryptography
    set "RC=1"
    goto :finish
  )
)

if not defined ARG set "ARG=panel"
"%PY%" "%ROOT%csx.py" %ARG%
set "RC=%ERRORLEVEL%"
goto :finish

:no_python
echo.
echo   [X] Bundled Python not found:
echo       %PY%
echo.
echo   The portable runtime is incomplete. Re-copy the whole folder,
echo   or keep the "runtime" subfolder next to this .bat file.
set "RC=1"

:finish
echo.
if "%NOPAUSE%"=="0" pause
exit /b %RC%
