@echo off
rem Сборка единого Downstream-Agent.exe (нужен Python 3.11+ для Windows). Результат: dist\Downstream-Agent.exe
rem ВНИМАНИЕ: если рядом лежит .env, он вшивается в exe вместе с ключами — не распространяйте файл за пределы доверенных людей.
cd /d "%~dp0"
if not exist .winbuild\Scripts\python.exe (
  python -m venv .winbuild || exit /b 1
)
.winbuild\Scripts\python -m pip install -q --upgrade pip
.winbuild\Scripts\python -m pip install -q requests lxml pymupdf openpyxl ddgs pypdf certifi playwright pyinstaller || exit /b 1
set ENVOPT=
if exist .env set ENVOPT=--add-data ".env;."
.winbuild\Scripts\python -m PyInstaller --noconfirm --clean --onefile --name Downstream-Agent --noconsole ^
  --add-data "downstream_agent\web;web" --add-data "certs\ca_bundle.pem;certs" --add-data "companies.toml;." --add-data "tesseract_win;tesseract" %ENVOPT% ^
  --collect-all ddgs --collect-all pymupdf --collect-all playwright --collect-submodules downstream_agent --hidden-import lxml.etree --hidden-import lxml.html ^
  app_entry.py || exit /b 1
echo.
echo Готово: dist\Downstream-Agent.exe
