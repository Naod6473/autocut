@echo off
REM Fabrique dist\Autocut.exe sur Windows. Il faut Python 3.10 ou plus (python.org, case "Add to PATH" cochée).
cd /d "%~dp0"
if not exist .venv (
    python -m venv .venv || goto :erreur
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-dev.txt || goto :erreur
pyinstaller --noconfirm --clean autocut.spec || goto :erreur
echo.
echo Termine : dist\Autocut.exe
pause
exit /b 0
:erreur
echo.
echo La fabrication a echoue, voir les messages ci-dessus.
pause
exit /b 1
