"""Настройки Karman. Читаются из .env рядом с файлом, без сторонних библиотек.

На хостинге .env нет — всё то же самое задаётся переменными окружения сервиса.
"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def _load_env(path: Path) -> None:
    """Минимальный парсер .env: KEY=VALUE, # комментарии, кавычки снимаются.

    Переменные, уже заданные в окружении, не перезаписываются — так тесты
    могут подменить DB_PATH до импорта модуля.
    """
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


_load_env(ENV_PATH)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# База: Postgres по DATABASE_URL (облако) или SQLite по DB_PATH (локально и тесты).
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = Path(os.environ.get("DB_PATH") or BASE_DIR / "karman.db")

# Стартовый демо-капитал в долларах и максимум одной сделки.
START_CASH = float(os.environ.get("START_CASH", "10000"))
MAX_TRADE_USD = float(os.environ.get("MAX_TRADE_USD", "1000000"))

# Кран в «Получить» и ставка демо-стейкинга TON (годовых).
TOPUP_USD = float(os.environ.get("TOPUP_USD", "1000"))
STAKE_APY = float(os.environ.get("STAKE_APY", "0.045"))

# Сколько секунд держать цены в кэше, чтобы не упираться в лимиты CoinGecko.
PRICE_TTL = int(os.environ.get("PRICE_TTL", "60"))
CHART_TTL = int(os.environ.get("CHART_TTL", "300"))

# Как часто серверный автотрейдер проходит по всем включённым ботам (сек).
BOT_TICK = int(os.environ.get("BOT_TICK", "180"))

APP_NAME = "Karman"

# HTTP-сервер: API для Mini App и раздача app/. Хостинги (Render и т.п.) передают порт в PORT.
APP_PORT = int(os.environ.get("PORT") or os.environ.get("APP_PORT", "8080"))

# Бесплатные хостинги усыпляют сервис без входящих запросов. Если задан адрес (обычно
# свой же /health), процесс сам дёргает его раз в KEEPALIVE_EVERY секунд.
KEEPALIVE_URL = os.environ.get("KEEPALIVE_URL", "").strip()
KEEPALIVE_EVERY = int(os.environ.get("KEEPALIVE_EVERY", "600"))

# Режим разработки: запросы к API без подписи Telegram считаются этим пользователем.
# Нужен, чтобы открыть приложение в обычном браузере с localhost. В облаке не задавать.
DEV_USER_ID = int(os.environ.get("DEV_USER_ID") or 0)

# Публичный HTTPS-адрес Mini App. Берётся из .env (WEBAPP_URL) или из webapp_url.txt —
# туда его пишет serve.py, когда поднимает туннель; файл читается при каждом обращении,
# поэтому бота после запуска туннеля перезапускать не нужно. На хостинге WEBAPP_URL —
# адрес самого сервиса: приложение раздаёт он же.
WEBAPP_URL_FILE = BASE_DIR / "webapp_url.txt"


def webapp_url() -> str:
    url = os.environ.get("WEBAPP_URL", "").strip()
    if not url and WEBAPP_URL_FILE.exists():
        # utf-8-sig: Блокнот и PowerShell пишут BOM, с ним startswith("https://") не сработает.
        url = WEBAPP_URL_FILE.read_text(encoding="utf-8-sig").strip()
    if not url.startswith("https://"):
        return ""
    return url if url.endswith("/") else url + "/"
