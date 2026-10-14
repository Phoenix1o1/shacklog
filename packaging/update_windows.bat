@echo off
rem ------------------------------------------------------------------------------------
rem  Update ShackLog on Windows: git pull, rebuild ShackLog.exe, start it again.
rem
rem  Started by Settings > Program & updates > "Update now" (with the PID of the running
rem  ShackLog, which it waits for), or by double-clicking it. Your data folder
rem  (%LOCALAPPDATA%\ShackLog) is never touched.
rem
rem  Needs: Git for Windows (winget install Git.Git) and Python 3.11+ (python.org).
rem ------------------------------------------------------------------------------------
setlocal EnableExtensions
title ShackLog update
cd /d "%~dp0\.."
set "REPO=%CD%"
echo.
echo  ShackLog update
echo  Folder: %REPO%
echo.

rem 1. Wait for the running copy to close (it exits right after starting this script).
if not "%~1"=="" (
  echo Waiting for ShackLog ^(PID %~1^) to close...
  for /l %%i in (1,1,60) do (
    tasklist /fi "PID eq %~1" 2>nul | find "%~1" >nul || goto :closed
    timeout /t 1 /nobreak >nul
  )
  echo It didn't close in 60 seconds. Close it, then run this script again.
  goto :fail
)
:closed
tasklist /fi "IMAGENAME eq ShackLog.exe" 2>nul | find /i "ShackLog.exe" >nul && (
  echo ShackLog.exe is still running. Close it ^(Settings ^> Quit^), then press a key.
  pause >nul
)

rem 2. Get the latest version.
where git >nul 2>nul || (
  if exist "%ProgramFiles%\Git\cmd\git.exe" (set "PATH=%ProgramFiles%\Git\cmd;%PATH%") else (
    echo Git is not installed. Install it with:  winget install Git.Git
    goto :fail
  )
)
for /f %%c in ('git rev-parse --short HEAD') do set "OLD=%%c"
echo Pulling from GitHub...
git pull --ff-only || (
  echo.
  echo git pull failed. If files were changed by hand, see "git status" in this folder.
  goto :restart_old
)
for /f %%c in ('git rev-parse --short HEAD') do set "NEW=%%c"
rem  Everything above this line must stay exactly as it is. cmd.exe reads a batch file
rem  from disk as it runs, and an older copy of this script that is busy pulling this
rem  version continues at the same byte offset in the new text. (ShackLog now runs a
rem  copy of this script from dist\, which git never changes.)
rem
rem  2nd argument "rebuild": ShackLog.exe was built from an older commit than the
rem  source (an earlier update pulled but couldn't build), so build even if git pull
rem  brought nothing new.
if "%OLD%"=="%NEW%" if exist dist\ShackLog.exe if /i not "%~2"=="rebuild" (
  echo Already up to date ^(%NEW%^).
  goto :start
)
echo Updated %OLD% -^> %NEW%
git --no-pager log --oneline %OLD%..%NEW% 2>nul

rem 3. Rebuild into dist-new, then swap, so a failed build leaves the old exe working.
where python >nul 2>nul || (echo Python is not installed ^(python.org^). & goto :restart_old)
if not exist .venv-build\Scripts\python.exe python -m venv .venv-build || goto :restart_old
call .venv-build\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt -r requirements-dev.txt || goto :restart_old
echo Running the tests...
python -m pytest -q tests || (echo Tests failed: keeping the old version. & goto :restart_old)
echo Building ShackLog.exe...
if exist dist-new rmdir /s /q dist-new
pyinstaller packaging\shacklog.spec --noconfirm --distpath dist-new --log-level WARN || goto :restart_old
if not exist dist mkdir dist
rem The old ShackLog.exe can still be held open for a moment after the program quit.
for /l %%i in (1,1,15) do (
  move /y dist-new\ShackLog.exe dist\ShackLog.exe >nul 2>nul && goto :moved
  timeout /t 1 /nobreak >nul
)
echo Couldn't replace dist\ShackLog.exe ^(still in use?^).
goto :restart_old
:moved
rmdir /s /q dist-new
echo Build complete.

:start
echo Starting ShackLog...
call :launch
timeout /t 3 /nobreak >nul
exit /b 0

:restart_old
echo.
if exist dist\ShackLog.exe (
  echo Starting the previous version again.
  call :launch
)
:fail
echo.
echo Update not completed. Press a key to close this window.
pause >nul
exit /b 1

:launch
rem Through Explorer, ShackLog starts fully detached from this window and with a clean
rem environment, so it keeps running when this window closes. Explorer can't pass
rem arguments, so a ShackLog that runs on a data folder given with --data-dir is passed
rem SHACKLOG_RELAUNCH_ARGS by the program and started with "start" instead
rem (PYINSTALLER_RESET_ENVIRONMENT, also set by the program, makes it unpack afresh).
if defined SHACKLOG_RELAUNCH_ARGS (
  start "" "%REPO%\dist\ShackLog.exe" %SHACKLOG_RELAUNCH_ARGS%
) else (
  explorer.exe "%REPO%\dist\ShackLog.exe"
)
exit /b 0
