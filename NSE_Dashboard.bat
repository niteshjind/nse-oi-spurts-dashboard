@echo off
title NSE OI Spurts Dashboard
echo.
echo  ========================================
echo    NSE OI Spurts Dashboard - Starting...
echo  ========================================
echo.

:: Start the Flask server in the background
start /B python "%~dp0server.py" > nul 2>&1

:: Wait for server to be ready
echo  Starting server...
timeout /t 3 /nobreak > nul

:: Open in Google Chrome
echo  Opening dashboard in Google Chrome...
if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" "http://localhost:5000"
) else if exist "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" (
    start "" "%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe" "http://localhost:5000"
) else (
    start http://localhost:5000
)

echo.
echo  ----------------------------------------
echo   Dashboard is running at localhost:5000
echo   Press any key to STOP the server.
echo  ----------------------------------------
echo.
pause > nul

:: Kill the Python server when user presses a key
taskkill /F /IM python.exe /FI "WINDOWTITLE eq NSE*" > nul 2>&1
taskkill /F /FI "WINDOWTITLE eq *server.py*" > nul 2>&1

:: Find and kill the specific Flask process on port 5000
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5000 ^| findstr LISTENING') do (
    taskkill /F /PID %%a > nul 2>&1
)

echo.
echo  Server stopped. You can close this window.
timeout /t 2 > nul
