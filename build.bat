@echo off
REM Build DropIt.exe - run this on your Windows PC
pip install -r requirements.txt
pyinstaller --noconfirm --onefile --windowed --name DropIt --icon assets\icon.ico app.py
echo.
echo Done! Your app is at dist\DropIt.exe - double-click to run.
pause
