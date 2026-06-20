@echo off
REM ===========================================================
REM  Zero Agent - launch the Chainlit split-screen UI
REM ===========================================================
setlocal
cd /d "%~dp0"
title ZERO Agent
color 0B

echo.
echo     ######  ######  ######   ######
echo        ##   ##      ##   ##  ##  ##
echo       ##    ####    ######   ##  ##
echo      ##     ##      ##   ##  ##  ##
echo     ######  ######  ##   ##   ######
echo.
echo            Z E R O   -   A I   A G E N T
echo     ===========================================
echo.

REM --- Locate the virtual environment -------------------------------------
set "VENV=%~dp0.venv"

if not exist "%VENV%\Scripts\activate.bat" (
    echo [ERROR] No virtual environment found.
    echo Looked in: "%~dp0.venv"
    echo Create one with:
    echo     python -m venv .venv
    echo     .venv\Scripts\pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

echo Activating environment: %VENV%
call "%VENV%\Scripts\activate.bat"

REM --- Default model (tool-capable + fast). Override by setting ZERO_AGENT_MODEL
if "%ZERO_AGENT_MODEL%"=="" set "ZERO_AGENT_MODEL=qwen3:32b"
REM --- Context window (tokens). Override by setting ZERO_AGENT_NUM_CTX ------
if "%ZERO_AGENT_NUM_CTX%"=="" set "ZERO_AGENT_NUM_CTX=16384"
REM --- Local login for the saved-chats sidebar (stays on this machine) -----
if "%ZERO_AGENT_USER%"=="" set "ZERO_AGENT_USER=admin"
if "%ZERO_AGENT_PASSWORD%"=="" set "ZERO_AGENT_PASSWORD=zero"
if "%CHAINLIT_AUTH_SECRET%"=="" set "CHAINLIT_AUTH_SECRET=zero-agent-local-secret-change-me"
echo Using model: %ZERO_AGENT_MODEL%
echo Context window: %ZERO_AGENT_NUM_CTX% tokens
echo Login: %ZERO_AGENT_USER% / %ZERO_AGENT_PASSWORD%  (local only)

echo Starting Zero Agent (Chainlit) on http://localhost:8000 ...
echo.
chainlit run app.py -w

REM --- Keep the window open if Chainlit exits with an error ----------------
if errorlevel 1 (
    echo.
    echo [ERROR] Chainlit exited with code %errorlevel%.
    pause
)

endlocal
