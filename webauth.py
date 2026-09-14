"""Кто стучится в API. Два способа:

1. Mini App внутри Telegram присылает `initData` — строку, подписанную ключом от токена
   бота (https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
   Заголовок: `Authorization: tma <initData>`.
2. Ссылка «Открыть в браузере» из чата несёт одноразовый токен пользователя:
   `?t=<user_id>.<expires>.<hmac>`. Приложение хранит его в localStorage и шлёт как
   `Authorization: tok <token>`. Секрет — тот же токен бота; срок — 30 дней.

Оба способа дают только id и имя пользователя; всё остальное берётся из БД.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

from config import BOT_TOKEN

INIT_DATA_MAX_AGE = 24 * 3600
BROWSER_TOKEN_TTL = 30 * 24 * 3600


@dataclass(frozen=True)
class WebUser:
    id: int
    first_name: str | None = None
    username: str | None = None


def _secret_key() -> bytes:
    return hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()


def parse_init_data(init_data: str, *, now: float | None = None) -> WebUser | None:
    """Проверяет подпись и свежесть initData. None — если что-то не так."""
    if not init_data or not BOT_TOKEN:
        return None
    pairs = parse_qsl(init_data, keep_blank_values=True)
    fields = dict(pairs)
    received = fields.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    expected = hmac.new(_secret_key(), check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        auth_date = int(fields.get("auth_date", "0"))
        user = json.loads(fields.get("user", "{}"))
        uid = int(user["id"])
    except (ValueError, KeyError, TypeError):
        return None
    if (now or time.time()) - auth_date > INIT_DATA_MAX_AGE:
        return None
    return WebUser(uid, user.get("first_name"), user.get("username"))


def _sign(payload: str) -> str:
    return hmac.new(BOT_TOKEN.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def browser_token(user_id: int, *, now: float | None = None) -> str:
    expires = int((now or time.time()) + BROWSER_TOKEN_TTL)
    payload = f"{user_id}.{expires}"
    return f"{payload}.{_sign(payload)}"


def parse_browser_token(token: str, *, now: float | None = None) -> WebUser | None:
    if not token or not BOT_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    uid, expires, sig = parts
    if not hmac.compare_digest(_sign(f"{uid}.{expires}"), sig):
        return None
    try:
        if int(expires) < (now or time.time()):
            return None
        return WebUser(int(uid))
    except ValueError:
        return None


def authenticate(header: str | None) -> WebUser | None:
    """Разбирает заголовок Authorization. `tma <initData>` или `tok <token>`."""
    if not header or " " not in header:
        return None
    kind, value = header.split(" ", 1)
    kind = kind.lower()
    if kind == "tma":
        return parse_init_data(value.strip())
    if kind == "tok":
        return parse_browser_token(value.strip())
    return None
