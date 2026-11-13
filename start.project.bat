
@echo off
setlocal
title CrisisGuard Demo Startup
set "VERIFY_MODE=0"
set "START_PROJECT=crisisguard-local"
if /I "%~1"=="--verify" (
    set "VERIFY_MODE=1"
    set "START_PROJECT=cg-launcher-%RANDOM%%RANDOM%"
) else if not "%~1"=="" (
    echo Usage: start.project.bat [--verify]
    exit /b 2
)

cd /d "%~dp0"
if errorlevel 1 goto failed

echo ========================================
echo       CrisisGuard Local Demo
echo ========================================
echo.

if not exist "compose.yaml" (
    echo ERROR: compose.yaml is missing.
    goto failed
)

if not exist "infrastructure\docker\compose.metaguard.yaml" (
    echo ERROR: MetaGuard Compose overlay is missing.
    goto failed
)

if not exist "infrastructure\docker\compose.dashboard-test-traffic.local.yaml" (
    echo ERROR: Local test traffic Compose overlay is missing.
    goto failed
)

if not exist "infrastructure\scripts\init_local_env.py" (
    echo ERROR: Local environment initializer is missing.
    goto failed
)

set "PROJECT_PYTHON=.venv311\Scripts\python.exe"

if not exist "%PROJECT_PYTHON%" (
    echo ERROR: Python virtual environment missing.
    echo Run: py -3.11 -m venv .venv311
    echo Then install requirements.txt using .venv311\Scripts\python.exe -m pip.
    goto failed
)

"%PROJECT_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) and sys.prefix != sys.base_prefix else 1)"

if errorlevel 1 (
    echo ERROR: CrisisGuard requires a Python 3.11 virtual environment at .venv311.
    goto failed
)
echo Checking host dependencies with "%CD%\%PROJECT_PYTHON%"...
"%PROJECT_PYTHON%" -c "import httpx, fastapi, uvicorn; from pydantic import BaseModel, ConfigDict; from infrastructure.scripts import local_outage_agent"
if errorlevel 1 (
    echo ERROR: Host Python dependency check failed. Install the declared requirements and retry.
    echo Run in the documented setup shell:
    echo   ^& "%CD%\%PROJECT_PYTHON%" -m pip install -r "%CD%\requirements.txt"
    echo   ^& "%CD%\%PROJECT_PYTHON%" -m pip check
    goto failed
)

set "DOCKER_ENDPOINT="
for /f "delims=" %%H in ('docker context inspect --format "{{.Endpoints.docker.Host}}" 2^>nul') do set "DOCKER_ENDPOINT=%%H"
if /I not "%DOCKER_ENDPOINT:~0,17%"=="npipe:////./pipe/" (
    echo ERROR: The local launcher requires a Windows Docker Desktop named pipe context.
    goto failed
)
if not defined DOCKER_HOST goto docker_host_checked
if /I not "%DOCKER_HOST:~0,17%"=="npipe:////./pipe/" (
    echo ERROR: A remote DOCKER_HOST is not allowed for local test traffic.
    goto failed
)
:docker_host_checked

echo.
echo Preparing private local configuration...
"%PROJECT_PYTHON%" "infrastructure\scripts\init_local_env.py" --with-local-test-traffic
if errorlevel 1 (
    echo ERROR: Local environment configuration is invalid.
    goto failed
)

docker info >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker Desktop is not running.
    goto failed
)

if not exist "db-snapshots" mkdir "db-snapshots"

set "COMPOSE_PROFILES="
set "COMPOSE_ARGS=-p %START_PROJECT% -f compose.yaml -f infrastructure/docker/compose.metaguard.yaml -f infrastructure/docker/compose.dashboard-test-traffic.local.yaml --profile metaguard"
echo Compose project: %START_PROJECT%

if exist "compose.override.yaml" (
    echo Using local Compose override.
    set "COMPOSE_ARGS=%COMPOSE_ARGS% -f compose.override.yaml"
) else (
    echo No override found. Using standard Compose.
)

echo.
echo Validating protected local Compose configuration...
docker compose %COMPOSE_ARGS% config --quiet
if errorlevel 1 (
    echo ERROR: Docker Compose configuration is invalid.
    goto failed
)

echo.
echo Starting all eight CrisisGuard services with MetaGuard protection...
docker compose %COMPOSE_ARGS% up -d --build --wait postgres patient emergency admin controller hospital-a hospital-b dashboard
if errorlevel 1 (
    echo ERROR: Docker Compose startup failed.
    goto failed
)

echo.
echo Creating Hospital A SQLite snapshot...
docker compose %COMPOSE_ARGS% exec -T hospital-a python -c "import sqlite3; s=sqlite3.connect('/data/hospital.db'); d=sqlite3.connect('/tmp/hospital-a-snapshot.db'); s.backup(d); d.close(); s.close()"
if errorlevel 1 goto snapshot_failed
docker compose %COMPOSE_ARGS% cp hospital-a:/tmp/hospital-a-snapshot.db ./db-snapshots/hospital-a.db
if errorlevel 1 goto snapshot_failed

echo.
echo Creating Hospital B SQLite snapshot...
docker compose %COMPOSE_ARGS% exec -T hospital-b python -c "import sqlite3; s=sqlite3.connect('/data/hospital.db'); d=sqlite3.connect('/tmp/hospital-b-snapshot.db'); s.backup(d); d.close(); s.close()"
if errorlevel 1 goto snapshot_failed
docker compose %COMPOSE_ARGS% cp hospital-b:/tmp/hospital-b-snapshot.db ./db-snapshots/hospital-b.db
if errorlevel 1 goto snapshot_failed

echo.
echo SQLite snapshots saved to db-snapshots.
goto start_agent

:snapshot_failed
echo WARNING: SQLite snapshot export failed.
echo The Docker services are still running.

:start_agent
if "%VERIFY_MODE%"=="1" (
    echo Verification startup completed for %START_PROJECT%.
    echo Browser and outage agent are skipped in verification mode.
    exit /b 0
)
echo.
echo Starting local Cloud Outage Agent...
start "CrisisGuard Cloud Outage Agent" /D "%CD%" "%ComSpec%" /k ""%CD%\%PROJECT_PYTHON%" -m infrastructure.scripts.local_outage_agent --allow-local-demo"
echo.
echo Opening dashboard...
start "" "http://localhost:8080"

echo.
echo ========================================
echo        CrisisGuard Demo Ready
echo ========================================
echo.
echo Hospital A: http://localhost:8000/docs
echo Hospital B: http://localhost:8002/docs
echo Patient:    http://localhost:8001/docs
echo Emergency:  http://localhost:8003/docs
echo Admin:      http://localhost:8004/docs
echo MetaGuard:  http://localhost:8005/health
echo Dashboard:  http://localhost:8080
echo.
echo SQLite snapshots: db-snapshots
echo.
echo Copy the token from the agent terminal
echo into the dashboard to enable outage controls.
echo.
pause
exit /b 0

:failed
echo.
echo ERROR: Demo startup failed.
if "%VERIFY_MODE%"=="0" pause
exit /b 1
