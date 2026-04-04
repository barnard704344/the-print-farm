@echo off
REM The Print Farm - Upload to farm after slicing
REM Paste into OrcaSlicer Post-processing Scripts:
REM   C:\Users\USERNAME\Downloads\upload_to_farm.bat

set "FARM_URL=http://0941-webserver.seatonhs.internal/the-print-farm"
set "API_KEY=the-print-farm-2026"
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

set "GCODE_DIR=%~dp1"
set "PARENT_DIR=%GCODE_DIR%.."
set "THUMB="

REM Try to extract thumbnail from the .3mf archive in the parent directory
for %%F in ("%PARENT_DIR%\*.3mf") do (
    if not defined THUMB (
        echo [%date% %time%] Found 3mf: %%F >> "%LOGFILE%"
        echo [%date% %time%] --- 3mf contents --- >> "%LOGFILE%"
        powershell -NoProfile -Command "try { Add-Type -AssemblyName System.IO.Compression.FileSystem; $z = [System.IO.Compression.ZipFile]::OpenRead('%%F'); foreach ($e in $z.Entries) { Write-Output ('{0}  ({1} bytes)' -f $e.FullName, $e.Length) }; $z.Dispose() } catch { Write-Output ('ERROR: ' + $_.Exception.Message) }" >> "%LOGFILE%" 2>&1
        echo [%date% %time%] --- end 3mf contents --- >> "%LOGFILE%"
        set "THUMB_EXTRACT=%TEMP%\farm_thumb_%RANDOM%.png"
        powershell -NoProfile -Command "try { Add-Type -AssemblyName System.IO.Compression.FileSystem; $z = [System.IO.Compression.ZipFile]::OpenRead('%%F'); foreach ($e in $z.Entries) { if ($e.FullName -match '\.png$') { $s = $e.Open(); $f = [System.IO.File]::Create('%THUMB_EXTRACT%'); $s.CopyTo($f); $f.Close(); $s.Close(); Write-Output $e.FullName; break } }; $z.Dispose() } catch { Write-Output 'EXTRACT_FAILED' }" > "%TEMP%\farm_thumb_result.txt" 2>&1
        type "%TEMP%\farm_thumb_result.txt" >> "%LOGFILE%"
        if exist "%THUMB_EXTRACT%" (
            set "THUMB=%THUMB_EXTRACT%"
            echo [%date% %time%] Extracted thumbnail to %THUMB_EXTRACT% >> "%LOGFILE%"
        ) else (
            echo [%date% %time%] No thumbnail in 3mf >> "%LOGFILE%"
        )
    )
)

REM Fallback: check for loose PNG files
if not defined THUMB (
    if exist "%GCODE_DIR%plate_1.png" set "THUMB=%GCODE_DIR%plate_1.png"
)
if not defined THUMB (
    if exist "%PARENT_DIR%\Metadata\plate_1.png" set "THUMB=%PARENT_DIR%\Metadata\plate_1.png"
)

if defined THUMB (
    echo [%date% %time%] Uploading with thumbnail: %THUMB% >> "%LOGFILE%"
    curl -s -X POST "%FARM_URL%/api/jobs/upload" -H "X-Api-Key: %API_KEY%" -F "file=@%GCODE%" -F "thumbnail=@%THUMB%" > "%TEMP%\farm_upload_result.txt" 2>&1
) else (
    echo [%date% %time%] No thumbnail found >> "%LOGFILE%"
    curl -s -X POST "%FARM_URL%/api/jobs/upload" -H "X-Api-Key: %API_KEY%" -F "file=@%GCODE%" > "%TEMP%\farm_upload_result.txt" 2>&1
)

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
