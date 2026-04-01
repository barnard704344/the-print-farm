@echo off
REM BambuLab Print Farm - Upload to farm after slicing
REM Paste into OrcaSlicer Post-processing Scripts:
REM   C:\Users\USERNAME\Downloads\upload_to_farm.bat

set "FARM_URL=http://0941-webserver.seatonhs.internal/bambulab-farm"
set "API_KEY=bambulab-farm-2026"
set "LOGFILE=%USERPROFILE%\Downloads\farm_upload.log"
set "GCODE=%~1"

echo [%date% %time%] Script started >> "%LOGFILE%"
echo [%date% %time%] Argument: %GCODE% >> "%LOGFILE%"

if "%GCODE%"=="" (
    echo [%date% %time%] ERROR: No file path provided >> "%LOGFILE%"
    exit /b 1
)

if not exist "%GCODE%" (
    echo [%date% %time%] ERROR: File not found: %GCODE% >> "%LOGFILE%"
    exit /b 1
)

echo [%date% %time%] Uploading %~nx1 ... >> "%LOGFILE%"
curl -s -X POST "%FARM_URL%/api/jobs/upload" -H "X-Api-Key: %API_KEY%" -F "file=@%GCODE%" > "%TEMP%\farm_upload_result.txt" 2>&1

echo [%date% %time%] Curl exit code: %ERRORLEVEL% >> "%LOGFILE%"
type "%TEMP%\farm_upload_result.txt" >> "%LOGFILE%"
echo. >> "%LOGFILE%"

findstr /C:"\"ok\":true" "%TEMP%\farm_upload_result.txt" >nul 2>&1
if %ERRORLEVEL%==0 (
    echo [%date% %time%] Upload SUCCESS >> "%LOGFILE%"
) else (
    echo [%date% %time%] Upload FAILED >> "%LOGFILE%"
    exit /b 1
)
exit /b 0
