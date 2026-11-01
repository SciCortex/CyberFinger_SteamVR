@echo off
REM -- CyberFinger Fusion Studio -- Build Script --
REM
REM Builds dist\CyberFingerFusionStudio\ (CyberFingerFusionStudio.exe + _internal), runs the exe's self-test and
REM zips the folder into dist\CyberFingerFusionStudio.zip (unzip anywhere and run the exe; no Python needed).
REM
REM Prerequisites, in the Python 3.12 you build with:
REM   pip install -r requirements-fusion-studio-lock.txt pyinstaller
REM   (the versions the exe was tested with; requirements-fusion-studio.txt lists the same packages unpinned)
REM
REM PRIVACY: module paths end up in the exe. Build from a path without your user name, e.g. C:\Build\CyberFinger\.

cd /d "%~dp0"
echo ======================================================
echo   CyberFinger Fusion Studio -- Build
echo ======================================================

echo [1/5] Converting icon...
python -c "from PIL import Image; img = Image.open('assets/icon_32x32.png'); img.save('assets/icon.ico', format='ICO', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
if errorlevel 1 echo WARNING: Could not create the .ico file (pip install pillow).

echo [2/5] Cleaning previous build...
if exist "build\fusion_studio" rmdir /s /q "build\fusion_studio"
if exist "dist\CyberFingerFusionStudio" rmdir /s /q "dist\CyberFingerFusionStudio"
if exist "dist\CyberFingerFusionStudio.zip" del /f "dist\CyberFingerFusionStudio.zip"

echo [3/5] Building (PyInstaller)...
set PYTHONDONTWRITEBYTECODE=1
python -m PyInstaller fusion_studio.spec --noconfirm --log-level WARN
if errorlevel 1 (
    echo ERROR: PyInstaller build failed!
    goto :end_fail
)

echo [4/5] Self-test of the built exe (no hardware needed)...
start "" /wait "dist\CyberFingerFusionStudio\CyberFingerFusionStudio.exe" --selftest "%CD%\dist\selftest.txt"
set SELFTEST=%errorlevel%
type "dist\selftest.txt"
if not "%SELFTEST%"=="0" (
    echo ERROR: the self-test failed -- see dist\selftest.txt
    goto :end_fail
)
findstr /i /c:"%USERNAME%" "dist\CyberFingerFusionStudio\CyberFingerFusionStudio.exe" >nul 2>&1
if not errorlevel 1 echo WARNING: user name "%USERNAME%" found in the exe -- build from a neutral path, e.g. C:\Build\

echo [5/5] Zipping...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\CyberFingerFusionStudio' -DestinationPath 'dist\CyberFingerFusionStudio.zip' -Force"

echo.
echo ======================================================
echo   Build complete!
echo   Folder: dist\CyberFingerFusionStudio\CyberFingerFusionStudio.exe
echo   Zip:    dist\CyberFingerFusionStudio.zip
echo ======================================================
if "%NOPAUSE%"=="" pause
exit /b 0

:end_fail
if "%NOPAUSE%"=="" pause
exit /b 1
