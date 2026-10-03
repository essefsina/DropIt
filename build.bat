@echo off
REM Build DropIt: app exe, full installer, and stub installer - run on your Windows PC
REM Needs: Python 3, and Inno Setup 6 (for the installer step)

cd /d "%~dp0"

echo [1/5] Installing Python deps...
pip install -r requirements.txt
if errorlevel 1 goto :fail

echo [2/5] Generating version files from dropit.py...
python make_version_files.py
if errorlevel 1 goto :fail

echo [3/5] Building DropIt.exe with PyInstaller...
pyinstaller --noconfirm --onefile --windowed --name DropIt --icon icon.ico --version-file file_version_info.txt --add-data "icon.png;." app.py
if errorlevel 1 goto :fail

echo [4/5] Building the full installer...
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
  echo.
  echo NOTE: Inno Setup 6 not found - skipping installer builds.
  echo Install it from https://jrsoftware.org/isdl.php then run build.bat again.
  echo Your app is still at dist\DropIt.exe
  goto :done
)
"%ISCC%" dropit.iss
if errorlevel 1 goto :fail

echo [5/5] Building the stub installer (DropItInstaller.exe)...
python check_stub_url.py
if errorlevel 1 goto :nostub
pyinstaller --noconfirm --onefile --windowed --name DropItInstaller --icon icon.ico --add-data "icon.ico;." stub.py
if errorlevel 1 goto :fail
echo.
echo Stub built: dist\DropItInstaller.exe - post this file anywhere, it always installs the latest version.
goto :done

:nostub
echo.
echo Skipped the stub: DOWNLOAD_URL in stub.py is still a placeholder.
echo Set it to https://github.com/YOURNAME/DropIt/releases/latest/download/DropIt-Setup.exe
echo and run build.bat again.
goto :done

:fail
echo.
echo BUILD FAILED - see the error above.
pause
exit /b 1

:done
echo.
echo Outputs:
echo   dist-installer\DropIt-Setup.exe  - full installer, upload to each GitHub Release
echo   dist\DropItInstaller.exe         - tiny stub, post anywhere, always installs latest
pause
