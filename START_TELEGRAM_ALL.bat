@echo off
REM ===========================================================
REM  START_TELEGRAM_ALL - one click for the Telegram agent
REM  Starts (only if not already running):
REM    1. Ollama server   (local LLM brain)   - REQUIRED
REM    2. ComfyUI         (image generation)  - for images
REM    3. Zero Agent Telegram bot
REM  No web UI is started - this is the Telegram-only path.
REM ===========================================================
setlocal
cd /d "%~dp0"
title ZERO - Telegram Stack
color 0B

echo.
echo     ######  ######  ######   ######
echo        ##   ##      ##   ##  ##  ##
echo       ##    ####    ######   ##  ##
echo      ##     ##      ##   ##  ##  ##
echo     ######  ######  ##   ##   ######
echo.
echo          Z E R O   -   T E L E G R A M   S T A C K
echo     ===========================================================
echo.

REM --- 1. Ollama ----------------------------------------------------------
echo [1/3] Checking Ollama (port 11434)...
powershell -NoProfile -Command "try{(Invoke-WebRequest -Uri 'http://localhost:11434/api/tags' -TimeoutSec 3 -UseBasicParsing)|Out-Null; exit 0}catch{exit 1}"
if errorlevel 1 (
    echo       Ollama not running - starting it in a new window...
    start "Ollama" cmd /c "ollama serve"
    echo       Waiting for Ollama to come up...
    powershell -NoProfile -Command "for($i=0;$i -lt 20;$i++){try{(Invoke-WebRequest -Uri 'http://localhost:11434/api/tags' -TimeoutSec 2 -UseBasicParsing)|Out-Null; exit 0}catch{Start-Sleep -Seconds 2}}; exit 1"
) else (
    echo       Ollama is already running. OK
)
echo.

REM --- 2. ComfyUI ---------------------------------------------------------
echo [2/3] Checking ComfyUI (port 8188)...
powershell -NoProfile -Command "try{(Invoke-WebRequest -Uri 'http://127.0.0.1:8188/system_stats' -TimeoutSec 3 -UseBasicParsing)|Out-Null; exit 0}catch{exit 1}"
if errorlevel 1 (
    if exist "C:\AI-MEDIA-RTX5090\START_COMFYUI.bat" (
        echo       ComfyUI not running - starting it in a new window...
        start "ComfyUI" cmd /c "C:\AI-MEDIA-RTX5090\START_COMFYUI.bat"
    ) else (
        echo       [WARN] ComfyUI launcher not found - image generation unavailable.
    )
) else (
    echo       ComfyUI is already running. OK
)
echo.

REM --- 3. Telegram bot ----------------------------------------------------
echo [3/3] Starting the Zero Agent Telegram bot...
if "%ZERO_AGENT_MODEL%"=="" set "ZERO_AGENT_MODEL=hermes3:8b"
if "%ZERO_AGENT_NUM_CTX%"=="" set "ZERO_AGENT_NUM_CTX=16384"
echo       Model: %ZERO_AGENT_MODEL%
echo       (Token + allow-list come from zero-agent\.env)
echo.
call "%~dp0start_telegram.bat"

endlocal
