@echo off
rem Start the shopping agent (Windows). First run sets everything up.
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo First run: setting things up ^(this takes a minute or two^)...
  py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist .venv\Scripts\python.exe (
  echo Could not create a Python environment. Install Python 3.10 or newer from https://www.python.org/downloads/
  pause
  exit /b 1
)
.venv\Scripts\python -m pip install --quiet --upgrade pip
.venv\Scripts\python -m pip install --quiet -r requirements.txt
findstr /r /c:"^SHOP_BROWSER_CHANNEL=." .env >nul 2>nul || .venv\Scripts\python -m playwright install chromium

if not exist .env copy .env.example .env >nul
findstr /r /c:"^ANTHROPIC_API_KEY=." .env >nul 2>nul
if errorlevel 1 if "%ANTHROPIC_API_KEY%"=="" (
  echo.
  echo One more step: paste your Anthropic API key after ANTHROPIC_API_KEY= in the file that opens,
  echo save it, and run start.bat again.
  notepad .env
  pause
  exit /b 1
)

.venv\Scripts\python -m shopping_agent
pause
