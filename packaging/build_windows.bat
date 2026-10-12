@echo off
rem Build ShackLog.exe on Windows. Needs Python 3.11+ from python.org on PATH.
rem Result: dist\ShackLog.exe  (double-click it; data goes to %LOCALAPPDATA%\ShackLog,
rem or to a ShackLogData folder next to the exe if you create one: portable mode).

cd /d "%~dp0\.."
python -m venv .venv-build || goto :error
call .venv-build\Scripts\activate.bat
python -m pip install --upgrade pip || goto :error
pip install -r requirements.txt -r requirements-dev.txt || goto :error
python -m pytest -q || goto :error
pyinstaller packaging\shacklog.spec --noconfirm || goto :error
echo.
echo Built dist\ShackLog.exe
goto :eof

:error
echo Build failed.
exit /b 1
