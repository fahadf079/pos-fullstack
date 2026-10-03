@echo off
cd /d "%~dp0"
if not exist backups mkdir backups
python backend\backup.py >> backups\backup.log 2>&1
