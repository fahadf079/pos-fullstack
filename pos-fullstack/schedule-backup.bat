@echo off
rem Run ONCE (double-click). Makes Windows take a backup of the POS database every day at 02:00.
cd /d "%~dp0"
if not exist backups mkdir backups
schtasks /Create /SC DAILY /ST 02:00 /TN "POS daily backup" /TR "\"%~dp0backup.bat\"" /F
echo.
echo Done. Test it now with:  backup.bat   (a file appears in the backups folder; backups\backup.log shows results)
echo To remove the schedule:  schtasks /Delete /TN "POS daily backup" /F
pause
