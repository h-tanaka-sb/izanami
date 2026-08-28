@echo off
rem ===== IZANAMI launcher (console visible - for debugging). ASCII only. =====
chcp 65001 >nul
cd /d "%~dp0"

set "UV_CMD=uv"
if exist "%~dp0_tools\uv.exe" set "UV_CMD=%~dp0_tools\uv.exe"

rem Verify packages too: a setup interrupted midway leaves python.exe with no
rem packages, and checking only the file would skip sync forever.
set "NEED_SYNC="
if not exist ".venv\Scripts\python.exe" set "NEED_SYNC=1"
if not defined NEED_SYNC (
  ".venv\Scripts\python.exe" -c "import importlib.util as u,sys; sys.exit(0 if all(u.find_spec(m) for m in ['sv_ttk','PIL','httpx','certifi','playwright','edge_tts','pykakasi','anthropic','openai','google.genai','googleapiclient','numpy']) else 1)" >nul 2>nul
  if errorlevel 1 set "NEED_SYNC=1"
)
if defined NEED_SYNC (
  echo First-time setup...
  "%UV_CMD%" sync --python 3.12
)

echo Launching IZANAMI (errors will show in this window)...
".venv\Scripts\python.exe" app.py
echo.
echo Finished.
pause
