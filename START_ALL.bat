@echo off
REM ===========================================================
REM  START_ALL - one click to launch the whole Zero Agent stack
REM  Starts (only if not already running):
REM    1. Ollama server      (local LLM brain)
REM    2. ComfyUI            (image / video generation)
REM    3. Zero Agent UI      (Chainlit on http://localhost:8000)
REM ===========================================================
setlocal
cd /d "%~dp0"
title ZERO - Full Stack
color 0B

echo.
echo     ######  ######  ######   ######
echo        ##   ##      ##   ##  ##  ##
echo       ##    ####    ######   ##  ##
echo      ##     ##      ##   ##  ##  ##
echo     ######  ######  ##   ##   ######
echo.
echo            Z E R O   -   F U L L   S T A C K
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
        echo       [WARN] ComfyUI launcher not found at C:\AI-MEDIA-RTX5090\START_COMFYUI.bat
        echo              Image generation will be unavailable until it is started.
    )
) else (
    echo       ComfyUI is already running. OK
)
echo.

REM --- 3. Zero Agent ------------------------------------------------------
echo [3/3] Starting Zero Agent UI...
if "%ZERO_AGENT_MODEL%"=="" set "ZERO_AGENT_MODEL=qwen3:32b"
REM Context window (tokens). 16384 = safe headroom on the 32GB card. A 28GB model
REM (qwen3.6) at 32768 fills VRAM (~98%) and a 2nd loaded model overflows -> freeze.
REM Small models (qwen3:4b) can go much higher; big ones keep <= ~16384.
if "%ZERO_AGENT_NUM_CTX%"=="" set "ZERO_AGENT_NUM_CTX=16384"
REM --- Local login for the saved-chats sidebar (stays on this machine) -----
if "%ZERO_AGENT_USER%"=="" set "ZERO_AGENT_USER=admin"
if "%ZERO_AGENT_PASSWORD%"=="" set "ZERO_AGENT_PASSWORD=zero"
if "%CHAINLIT_AUTH_SECRET%"=="" set "CHAINLIT_AUTH_SECRET=zero-agent-local-secret-change-me"
echo       Model:   %ZERO_AGENT_MODEL%
echo       Context: %ZERO_AGENT_NUM_CTX% tokens
echo       Login:   %ZERO_AGENT_USER% / %ZERO_AGENT_PASSWORD%  (local only)
echo.
call "%~dp0start_zero_agent.bat"

endlocal
