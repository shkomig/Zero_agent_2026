@echo off
REM ===========================================================
REM  START_SEARXNG - local metasearch for deep_research
REM  Starts a self-hosted SearXNG container (70+ engines, JSON
REM  API, no keys, no rate limits) on http://localhost:8888.
REM  The deep_research tool uses it automatically when it's up,
REM  and falls back to DuckDuckGo when it isn't.
REM
REM  Requires Docker Desktop to be running.
REM ===========================================================
setlocal
cd /d "%~dp0"
title ZERO - SearXNG
color 0B

echo.
echo     Z E R O   -   S E A R X N G   (local metasearch)
echo     ===========================================================
echo.

REM --- Check the Docker daemon is up --------------------------------------
docker info >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Docker is not running.
    echo Start Docker Desktop, wait for it to finish loading, then run this again.
    echo.
    pause
    exit /b 1
)

echo Starting SearXNG (first run downloads the image, ~1-2 min)...
docker compose -f docker-compose.searxng.yml up -d
if errorlevel 1 (
    echo.
    echo [ERROR] docker compose failed - see the message above.
    pause
    exit /b 1
)

echo.
echo SearXNG is starting on http://localhost:8888
echo Give it ~10 seconds, then deep_research will use it automatically.
echo (To stop it: docker compose -f docker-compose.searxng.yml down)
echo.
pause
endlocal
