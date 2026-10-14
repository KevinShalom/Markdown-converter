@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Verificando dependencias...
py -3.14 -m pip install -q -r requirements-obsidian.txt
py -3.14 -c "import markitdown" 2>nul || py -3.14 -m pip install -q -e "packages\markitdown[all]"
rem OCR: motor nativo de Windows y conector de Tesseract (si fallan, la app sigue funcionando sin OCR)
py -3.14 -c "import winocr" 2>nul || py -3.14 -m pip install -q winocr 2>nul
py -3.14 -c "import pytesseract" 2>nul || py -3.14 -m pip install -q pytesseract 2>nul
echo.
echo Servidor PDF -^> Obsidian en http://localhost:8000  (cierra esta ventana para detenerlo)
py -3.14 app.py
pause
