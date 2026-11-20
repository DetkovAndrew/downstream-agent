"""Пути приложения: рядом с исходниками при обычном запуске и рядом с .exe в собранном виде."""
from __future__ import annotations

import sys
from pathlib import Path


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Папка с данными пользователя: data/, .env, companies.toml (у .exe — папка, где он лежит)."""
    return Path(sys.executable).resolve().parent if frozen() else Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    """Папка со встроенными ресурсами (web/, certs/, companies.toml по умолчанию)."""
    return Path(getattr(sys, "_MEIPASS", app_dir())) if frozen() else app_dir()
