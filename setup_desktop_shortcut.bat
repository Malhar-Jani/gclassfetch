@echo off
setlocal
echo Creating Desktop Shortcut for gclassfetch...

set "SCRIPT_DIR=%~dp0"
set "BAT_PATH=%SCRIPT_DIR%run.bat"
set "VBS_SCRIPT=%TEMP%\create_shortcut.vbs"

echo Set oWS = WScript.CreateObject("WScript.Shell") > "%VBS_SCRIPT%"
echo sLinkFile = oWS.SpecialFolders("Desktop") ^& "\gclassfetch.lnk" >> "%VBS_SCRIPT%"
echo Set oLink = oWS.CreateShortcut(sLinkFile) >> "%VBS_SCRIPT%"
echo oLink.TargetPath = "%BAT_PATH%" >> "%VBS_SCRIPT%"
echo oLink.WorkingDirectory = "%SCRIPT_DIR%" >> "%VBS_SCRIPT%"
echo oLink.Description = "gclassfetch - Google Classroom Downloader" >> "%VBS_SCRIPT%"
echo oLink.Save >> "%VBS_SCRIPT%"

cscript /nologo "%VBS_SCRIPT%"
del "%VBS_SCRIPT%"

echo.
echo [OK] Shortcut created successfully on your Desktop!
echo.
pause
