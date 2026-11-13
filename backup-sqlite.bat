
@echo off
setlocal
title CrisisGuard SQLite Snapshot Export

cd /d "%~dp0"

echo ========================================
echo    CrisisGuard SQLite Snapshot Export
echo ========================================
echo.

docker info >nul 2>&1
if errorlevel 1 (
    echo ERROR: Docker Desktop is not running.
    goto failed
)

if not exist "db-snapshots" mkdir "db-snapshots"

echo Exporting Hospital A...
docker compose -p crisisguard-local -f compose.yaml exec -T hospital-a python -c "import sqlite3; s=sqlite3.connect('/data/hospital.db'); d=sqlite3.connect('/tmp/hospital-a-snapshot.db'); s.backup(d); d.close(); s.close()"
if errorlevel 1 goto failed

docker compose -p crisisguard-local -f compose.yaml cp hospital-a:/tmp/hospital-a-snapshot.db ./db-snapshots/hospital-a.db
if errorlevel 1 goto failed

echo Exporting Hospital B...
docker compose -p crisisguard-local -f compose.yaml exec -T hospital-b python -c "import sqlite3; s=sqlite3.connect('/data/hospital.db'); d=sqlite3.connect('/tmp/hospital-b-snapshot.db'); s.backup(d); d.close(); s.close()"
if errorlevel 1 goto failed

docker compose -p crisisguard-local -f compose.yaml cp hospital-b:/tmp/hospital-b-snapshot.db ./db-snapshots/hospital-b.db
if errorlevel 1 goto failed

echo.
echo SUCCESS: SQLite snapshots saved to:
echo %CD%\db-snapshots\
echo.
echo Open hospital-a.db or hospital-b.db in VS Code.
echo These are copies, not the live databases.
pause
exit /b 0

:failed
echo.
echo ERROR: Snapshot export failed.
echo Check that Hospital A and Hospital B are running.
pause
exit /b 1
