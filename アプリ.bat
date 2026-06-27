@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo AIニュースまとめアプリを起動します...
start "" http://127.0.0.1:8770/
python app_server.py
