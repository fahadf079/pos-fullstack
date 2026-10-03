@echo off
setlocal EnableExtensions
title POS launcher
cd /d "%~dp0"
echo.
echo ================= POS launcher =================
echo  Installs anything that is missing, then starts the
echo  backend and the screen. First run takes a few minutes.
echo ================================================
echo.

rem ---------- 1. Python and Node.js must be installed ----------
where python >nul 2>nul
if errorlevel 1 goto nopython
where npm >nul 2>nul
if errorlevel 1 goto nonode

rem ---------- 2. Backend packages (first run only) ----------
python -c "import fastapi, uvicorn, psycopg, psycopg_pool, tzdata" >nul 2>nul
if not errorlevel 1 goto backend_pkgs_ok
echo [1/4] Installing backend packages (first run only)...
python -m pip install -r backend\requirements.txt
if errorlevel 1 goto pipfail
:backend_pkgs_ok
echo [1/4] Backend packages: OK

rem ---------- 3. Frontend packages (first run only): this is the "vite is not recognized" fix ----------
if exist "frontend\node_modules\.bin\vite.cmd" goto frontend_pkgs_ok
echo [2/4] Installing frontend packages (first run only, needs internet, 1-3 minutes)...
call npm install --prefix frontend --no-audit --no-fund
if errorlevel 1 goto npmfail
:frontend_pkgs_ok
echo [2/4] Frontend packages: OK

rem ---------- 4. PostgreSQL must be answering on port 5432 ----------
python -c "import socket; socket.create_connection(('127.0.0.1',5432),2)" >nul 2>nul
if not errorlevel 1 goto db_ok
echo [3/4] PostgreSQL is not answering yet. Trying to start it...
for /f "tokens=2" %%S in ('sc query state^= all ^| findstr /i /c:"SERVICE_NAME: postgresql"') do net start %%S >nul 2>nul
where docker >nul 2>nul
if not errorlevel 1 docker compose up -d db >nul 2>nul
set /a DBTRIES=0
:dbwait
python -c "import socket; socket.create_connection(('127.0.0.1',5432),2)" >nul 2>nul
if not errorlevel 1 goto db_ok
set /a DBTRIES+=1
if %DBTRIES% GEQ 12 goto nodb
timeout /t 2 >nul
goto dbwait
:db_ok
echo [3/4] PostgreSQL: OK

rem ---------- 5. Start the backend in its own window and wait until it answers ----------
echo [4/4] Starting the backend and the screen...
start "POS backend" /d "%~dp0backend" cmd /k python -m uvicorn main:app --reload --port 8000 --timeout-graceful-shutdown 2
set /a TRIES=0
:waitbackend
python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/auth/status', timeout=2)" >nul 2>nul
if not errorlevel 1 goto backend_ok
set /a TRIES+=1
if %TRIES% GEQ 40 goto backendfail
timeout /t 1 >nul
goto waitbackend
:backend_ok

rem ---------- 6. Start the screen and open the browser ----------
start "POS frontend" /d "%~dp0frontend" cmd /k npm run dev
timeout /t 5 >nul
start "" http://localhost:5173
echo.
echo  Running. The POS opens in your browser: http://localhost:5173
echo  Keep the two black windows (POS backend, POS frontend) open while you work.
echo  The very first time you will see First-time setup: create the owner account.
echo.
timeout /t 8 >nul
exit /b 0

:nopython
echo [X] Python was not found.
echo     Install Python 3 from https://www.python.org/downloads/ and TICK "Add python.exe to PATH" in the installer,
echo     then close this window and run start.bat again.
pause
exit /b 1

:nonode
echo [X] Node.js was not found.
echo     Install the LTS version from https://nodejs.org/ then close this window and run start.bat again.
pause
exit /b 1

:pipfail
echo [X] Installing the backend packages failed. Check your internet connection, then run start.bat again.
echo     Or run this by hand in this folder:  python -m pip install -r backend\requirements.txt
pause
exit /b 1

:npmfail
echo [X] Installing the frontend packages failed. Check your internet connection, then run start.bat again.
echo     Or run this by hand:  cd frontend   then   npm install
pause
exit /b 1

:nodb
echo [X] PostgreSQL is not running on port 5432.
echo     Press the Windows key, type  services  and open Services. Find the one named postgresql-x64-18
echo     (the number is your PostgreSQL version), right-click it and choose Start. Then run start.bat again.
echo     If PostgreSQL was never installed, or the user/database "pos" was not created, see README.md section 1.
pause
exit /b 1

:backendfail
echo [X] The backend did not start within 40 seconds. Look at the black window titled "POS backend":
echo     the last lines say why (for example a wrong database password or a missing database).
pause
exit /b 1
