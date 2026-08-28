@echo off
rem ===== IZANAMI launcher (ASCII only - cmd parses this regardless of codepage) =====
chcp 65001 >nul
cd /d "%~dp0"

set "UV_CMD=uv"
if exist "%~dp0_tools\uv.exe" set "UV_CMD=%~dp0_tools\uv.exe"

rem First-run setup. uv creates python.exe BEFORE installing packages, so a
rem setup interrupted midway leaves python.exe with no packages. Checking only
rem the file would skip sync forever, so verify the packages too (self-healing).
set "NEED_SYNC="
if not exist ".venv\Scripts\python.exe" set "NEED_SYNC=1"
if not defined NEED_SYNC (
  ".venv\Scripts\python.exe" -c "import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ['sv_ttk','PIL','httpx','certifi','playwright','edge_tts','pykakasi','anthropic','openai','google.genai','googleapiclient','numpy']) else 1)" >nul 2>nul
  if errorlevel 1 set "NEED_SYNC=1"
)
if defined NEED_SYNC (
  echo ==============================================
  echo    IZANAMI setup
  echo ==============================================
  echo Installing Python 3.12 and packages, a few minutes...
  "%UV_CMD%" sync --python 3.12
)

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] setup failed. Check your internet connection and run again.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ['sv_ttk','PIL','httpx','certifi','playwright','edge_tts','pykakasi','anthropic','openai','google.genai','googleapiclient','numpy']) else 1)" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] setup is incomplete. Check your internet connection and run again.
  pause
  exit /b 1
)

rem Create desktop shortcut once (self-path via %~f0 avoids embedding non-ASCII)
if not exist "%~dp0.shortcut_created" (
  powershell -NoProfile -Command "$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\IZANAMI.lnk'); $s.TargetPath='%~f0'; $s.WorkingDirectory='%~dp0'; if (Test-Path '%~dp0assets\izanami.ico') { $s.IconLocation='%~dp0assets\izanami.ico' } elseif (Test-Path '%~dp0assets\icon.ico') { $s.IconLocation='%~dp0assets\icon.ico' }; $s.WindowStyle=7; $s.Description='IZANAMI'; $s.Save()" >nul 2>nul
  echo created> "%~dp0.shortcut_created"
)

rem Launch GUI hidden. python.exe via VBS (pythonw breaks Playwright/Chrome stdio).
start "" wscript.exe "%~dp0_run_hidden.vbs"
