@echo off
REM One-shot ELO setup for Windows — double-click me, or run from a terminal.
REM Builds the whole rating system (downloads data + trains the models).
REM Pass flags through, e.g.:  setup_elo.bat --quick
cd /d "%~dp0"

where py >nul 2>nul && (
    py scripts\elo\setup.py %*
) || (
    python scripts\elo\setup.py %*
)

echo.
echo Fenster kann geschlossen werden.
pause
