#!/usr/bin/env bash
# Installs what is missing, then starts the backend and the screen (Mac / Linux).
cd "$(dirname "$0")" || exit 1
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is not installed."; exit 1; }
command -v npm >/dev/null 2>&1 || { echo "Node.js (npm) is not installed."; exit 1; }
python3 -c "import fastapi, uvicorn, psycopg, psycopg_pool, tzdata" 2>/dev/null || { echo "Installing backend packages..."; python3 -m pip install -r backend/requirements.txt || exit 1; }
[ -x frontend/node_modules/.bin/vite ] || { echo "Installing frontend packages..."; npm install --prefix frontend --no-audit --no-fund || exit 1; }
# PostgreSQL via Docker, only if nothing answers on 5432 yet (skip if you run PostgreSQL yourself):
python3 -c "import socket; socket.create_connection(('127.0.0.1',5432),2)" 2>/dev/null || { command -v docker >/dev/null 2>&1 && docker compose up -d db && sleep 5; }
(cd backend && python3 -m uvicorn main:app --reload --port 8000 --timeout-graceful-shutdown 2) &
(cd frontend && npm run dev) &
wait
