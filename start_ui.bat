@echo off
rem Запуск интерфейса Downstream Agent (через WSL); затем откройте http://localhost:8501
wsl --cd "%~dp0" bash -lc ".venv/bin/python -m downstream_agent ui"
