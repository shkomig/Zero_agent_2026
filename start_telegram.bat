@echo off
REM ===========================================================
REM  Zero Agent - Telegram bridge
REM  Talk to the SAME local agent (tools + memory + projects)
REM  from Telegram. The model still runs locally on Ollama;
REM  only the chat transport goes through Telegram.
REM
REM  Setup (once):
REM    1. Create a bot with @BotFather, copy its token.
REM    2. Put the token + your Telegram ID in zero-agent\.env
REM       (copy .env.example), OR set them below before running.
REM ===========================================================
setlocal
cd /d "%~dp0"
title ZERO Agent - Telegram
color 0B

echo.
echo     ######  ######  ######   ######
echo        ##   ##      ##   ##  ##  ##
echo       ##    ####    ######   ##  ##
echo      ##     ##      ##   ##  ##  ##
echo     ######  ######  ##   ##   ######
echo.
echo            Z E R O   -   T E L E G R A M
echo     ===========================================
echo.

REM --- Locate the virtual environment -------------------------------------
set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\activate.bat" (
    echo [ERROR] No virtual environment found at "%~dp0.venv"
    echo Create one with:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)
call "%VENV%\Scripts\activate.bat"

REM --- Default model (tool-capable + fast). Override via ZERO_AGENT_TELEGRAM_MODEL
if "%ZERO_AGENT_MODEL%"=="" set "ZERO_AGENT_MODEL=hermes3:8b"
if "%ZERO_AGENT_NUM_CTX%"=="" set "ZERO_AGENT_NUM_CTX=16384"

echo Starting the Telegram bridge...
echo (Token + allow-list are read from zero-agent\.env or the environment.)
echo.
python telegram_bot.py

if errorlevel 1 (
    echo.
    echo [ERROR] Telegram bridge exited with code %errorlevel%.
    pause
)

endlocal
