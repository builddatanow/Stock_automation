@echo off
cd /d C:\Users\Administrator\Desktop\projects\dashboard
"C:\Program Files\Python311\python.exe" -m waitress --host=127.0.0.1 --port=5000 --threads=4 app:app
