@echo off
REM ============================================================
REM  Export Claude Science conversations
REM
REM  double click          -> pick output folder in a dialog
REM  this.bat --dry-run    -> pick folder, then preview only
REM  this.bat --out DIR    -> export to DIR directly (no dialog)
REM  this.bat --list       -> list conversations and chosen articles
REM
REM  Data source defaults to the current sandbox data dir
REM  (%CSSWITCH_HOME%\sandbox\data, else D:\CSSwitch-Win-Data\sandbox\data);
REM  override with --data-dir. Remaining options are forwarded to
REM  tools\cs_export.py (--force / --config ...).
REM
REM  Keep this file CRLF + ASCII-only (see .editorconfig): cmd.exe
REM  misparses LF endings and non-ASCII bytes.
REM ============================================================
setlocal EnableExtensions
title Export Claude Science Conversations

set "ROOT=%~dp0"
set "PY=%ROOT%runtime\python\python.exe"
if not exist "%PY%" set "PY=python"

set "EXPORT=%ROOT%tools\cs_export.py"
if not exist "%EXPORT%" set "EXPORT=%ROOT%..\tools\cs_export.py"
if not exist "%EXPORT%" goto :no_export

set "HAS_OUT="
echo %* | findstr /i /c:"--out" >nul 2>nul
if not errorlevel 1 set "HAS_OUT=1"

set "OUT_ARG="
if defined HAS_OUT goto :data
call :pick_folder
if not defined OUT goto :cancel
set "OUT_ARG=--out "%OUT%""

:data
set "HAS_DATA="
echo %* | findstr /i /c:"--data-dir" >nul 2>nul
if not errorlevel 1 set "HAS_DATA=1"

set "DATA_ARG="
if defined HAS_DATA goto :run
set "DATA=%CSSWITCH_HOME%\sandbox\data"
if not defined CSSWITCH_HOME set "DATA=D:\CSSwitch-Win-Data\sandbox\data"
if exist "%DATA%" set "DATA_ARG=--data-dir "%DATA%""

:run
echo.
if defined OUT goto :show_out
echo   Output folder: from --out argument
goto :show_data

:show_out
echo   Output folder: %OUT%

:show_data
if defined HAS_DATA goto :data_arg
if defined DATA_ARG goto :data_auto
echo   Data source  : auto-detect
goto :exec

:data_arg
echo   Data source  : from --data-dir argument
goto :exec

:data_auto
echo   Data source  : %DATA%

:exec
echo.
"%PY%" "%EXPORT%" %OUT_ARG% %DATA_ARG% %*
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo   [FAILED] exit code %RC%
echo Press any key to close...
pause >nul
endlocal
exit /b %RC%

:cancel
echo   [i] No folder selected, nothing exported.
echo.
pause
exit /b 0

:no_export
echo   [X] cs_export.py not found.
echo       Expected: tools\cs_export.py
echo.
pause
exit /b 1

:pick_folder
set "OUT="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Choose a folder for exported Claude Science conversations'; $d.ShowNewFolderButton=$true; if(Test-Path 'D:\Paper\articles'){$d.SelectedPath='D:\Paper\articles'}; if($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK){Write-Output $d.SelectedPath}" 2^>nul`) do set "OUT=%%I"
goto :eof
