@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo AIニュースを集めています...
python news_app.py
echo.
echo 完了しました。output\index.html を開きます。
start "" "%~dp0output\index.html"
pause
