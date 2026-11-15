@echo off
REM Compile TadoWidget.exe (sans console) avec PyInstaller.
REM Prerequis : Python 3 installe depuis python.org (case "Add Python to PATH").
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller pystray pillow || goto :err
python -c "import tado_widget as t; t.make_icon(256).save('icon.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])" || goto :err
python -m PyInstaller --onefile --noconsole --name TadoWidget --icon icon.ico tado_widget.py || goto :err
echo.
echo OK : dist\TadoWidget.exe
pause
exit /b 0
:err
echo ECHEC de la compilation.
pause
exit /b 1
