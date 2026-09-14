"""Весь SQL — здесь. Одно ленивое соединение; SQLite рядом с ботом или Postgres в облаке.

Деньги в демо-счёте — доллары в float. Для песочницы этого достаточно; ошибки
округления гасятся тем, что «продать всё» берёт точное число из строки holdings,
а не пересчитанный процент.

Две СУБД, один код. Postgres включается переменной DATABASE_URL (на бесплатных хостингах
диск не сохраняется, база живёт отдельно), без неё — SQLite из DB_PATH. Разница спрятана
в `tx()`/`_q()`: плейсхолдеры `?` переписываются в `%s`, строки всегда dict. SQL пишется
на общем подмножестве: ON CONFLICT есть в обеих, типы — TEXT / BIGINT / DOUBLE PRECISION.

Функции синхронные и без await внутри транзакции — это и есть гарантия атомарности
сделки (см. CLAUDE.md). Для Postgres каждый запрос — сетевой вызов из event loop;
для демо с десятками пользователей это нормально, база должна жить в том же регионе.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from coins import COINS
from config import DATABASE_URL, DB_PATH, MAX_TRADE_USD, STAKE_APY, START_CASH, TOPUP_USD

log = logging.getLogger("db")

PG = bool(DATABASE_URL)
_conn: Any = None

# Минимальная сделка в долларах и допуск на сравнение остатков.
MIN_TRADE_USD = 1.0
EPS = 1e-9
SIDES = ("buy", "sell", "swap", "send", "receive", "stake", "unstake")


# ---------- соединение ----------

def _dict_row(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {d[0]: v for d, v in zip(cursor.description, row)}


def _connect() -> Any:
    if PG:
        import psycopg
        from psycopg.rows import dict_row

        # prepare_threshold=None: без серверных prepared statements — они ломаются за пулером
        # Supabase/pgbouncer в transaction-режиме, а выигрыш для наших запросов ничтожный.
        return psycopg.connect(DATABASE_URL, row_factory=dict_row, autocommit=False,
                               connect_timeout=15, application_name="karman", prepare_threshold=None)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = _dict_row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def db() -> Any:
    global _conn
    if _conn is None or (PG and _conn.closed):
        _conn = _connect()
    return _conn


def close() -> None:
    global _conn
    if _conn is not None:
        try:
            _conn.close()
        finally:
            _conn = None


def _q(sql: str) -> str:
    return sql.replace("?", "%s") if PG else sql


class _Exec:
    """Обёртка над соединением: единый execute с `?` для обеих СУБД."""

    def __init__(self, conn: Any):
        self._c = conn

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self._c.execute(_q(sql), params)

    def one(self, sql: str, params: tuple = ()) -> dict | None:
        return self.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple = ()) -> list[dict]:
        return self.execute(sql, params).fetchall()


@contextmanager
def tx() -> Iterator[_Exec]:
    """Транзакция: `with tx() as c:` — коммит на выходе, откат при исключении.

    Чтение тоже идёт через tx(): в Postgres autocommit выключен, и запрос вне
    транзакции оставил бы соединение висеть в «idle in transaction». Если соединение
    с Postgres отвалилось (пул закрыл его по простою), закрываем своё — следующий
    вызов переподключится; текущий запрос падает, повторит его вызывающий.
    """
    if not PG:
        with db() as c:
            yield _Exec(c)
        return
    import psycopg

    try:
        with db().transaction():
            yield _Exec(db())
    except psycopg.OperationalError as e:
        log.warning("Postgres: соединение потеряно (%s), переподключусь при следующем запросе", e)
        close()
        raise


def _one(sql: str, params: tuple = ()) -> dict | None:
    with tx() as c:
        return c.one(sql, params)


def _all(sql: str, params: tuple = ()) -> list[dict]:
    with tx() as c:
        return c.all(sql, params)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ts_text(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ---------- схема ----------

_AUTOID = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS users (
    id          BIGINT PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    cash        DOUBLE PRECISION NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'usd',
    chart_days  INTEGER NOT NULL DEFAULT 7,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS holdings (
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    amount      DOUBLE PRECISION NOT NULL,
    invested    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, symbol)
);
CREATE TABLE IF NOT EXISTS trades (
    id          {_AUTOID},
    user_id     BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,
    amount      DOUBLE PRECISION NOT NULL,
    price       DOUBLE PRECISION NOT NULL,
    total       DOUBLE PRECISION NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS trades_user ON trades(user_id, id DESC);
CREATE TABLE IF NOT EXISTS bot_state (
    user_id     BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    data        TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

# Колонки, появившиеся после первой версии. Старые SQLite-базы дописываем на ходу.
_MIGRATIONS = [
    ("users", "address", "TEXT"),
    ("users", "topups", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "stake_amount", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ("users", "stake_invested", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ("users", "stake_since", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    ("trades", "ts", "DOUBLE PRECISION NOT NULL DEFAULT 0"),   # unix-секунды; бот дописывает сделки «в прошлое»
    ("trades", "to_symbol", "TEXT"),        # swap: что получили
    ("trades", "got", "DOUBLE PRECISION"),  # swap: сколько получили
    ("trades", "pnl", "DOUBLE PRECISION"),  # sell/swap: результат против себестоимости
    ("trades", "party", "TEXT"),            # send: кому, receive: откуда
    ("trades", "is_bot", "INTEGER NOT NULL DEFAULT 0"),
    ("trades", "note", "TEXT"),             # причина сделки автотрейдера
]


def _add_column(c: _Exec, table: str, column: str, decl: str) -> None:
    if PG:
        c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {decl}")
        return
    cols = {r["name"] for r in c.all(f"PRAGMA table_info({table})")}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with tx() as c:
        if PG:
            c.execute(SCHEMA)
        else:
            db().executescript(SCHEMA)
        for table, column, decl in _MIGRATIONS:
            _add_column(c, table, column, decl)
        # Старым сделкам без ts проставляем время из created_at, чтобы история сортировалась.
        for t in c.all("SELECT id, created_at FROM trades WHERE ts = 0"):
            ts = datetime.strptime(t["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            c.execute("UPDATE trades SET ts = ? WHERE id = ?", (ts, t["id"]))
        c.execute("CREATE INDEX IF NOT EXISTS trades_user_ts ON trades(user_id, ts DESC)")


# ---------- пользователи ----------

def _gen_address() -> str:
    abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    return "UQ" + "".join(secrets.choice(abc) for _ in range(46))


def ensure_user(user_id: int, username: str | None, first_name: str | None) -> dict:
    with tx() as c:
        c.execute(
            """INSERT INTO users (id, username, first_name, cash, created_at, address)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET username = excluded.username,
                                             first_name = excluded.first_name""",
            (user_id, username, first_name, START_CASH, _now(), _gen_address()),
        )
        # Пользователи из первой версии — без адреса.
        c.execute("UPDATE users SET address = ? WHERE id = ? AND address IS NULL", (_gen_address(), user_id))
    return get_user(user_id)


def get_user(user_id: int) -> dict | None:
    return _one("SELECT * FROM users WHERE id = ?", (user_id,))


def set_currency(user_id: int, currency: str) -> None:
    with tx() as c:
        c.execute("UPDATE users SET currency = ? WHERE id = ?", (currency, user_id))


def set_chart_days(user_id: int, days: int) -> None:
    with tx() as c:
        c.execute("UPDATE users SET chart_days = ? WHERE id = ?", (days, user_id))


def reset_account(user_id: int) -> None:
    """Обнуляет демо-счёт: монеты, стейкинг, история и результаты бота стираются,
    наличные — стартовые. Настройки бота (стратегия, бюджет) остаются."""
    with tx() as c:
        c.execute("DELETE FROM holdings WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM trades WHERE user_id = ?", (user_id,))
        c.execute(
            "UPDATE users SET cash = ?, topups = 0, stake_amount = 0, stake_invested = 0, stake_since = 0 WHERE id = ?",
            (START_CASH, user_id),
        )
        row = c.one("SELECT data FROM bot_state WHERE user_id = ?", (user_id,))
        if row:
            bot = json.loads(row["data"])
            for key in ("on", "spent", "lots", "log", "stats", "last", "lastTick"):
                bot.pop(key, None)
            c.execute("UPDATE bot_state SET data = ?, updated_at = ? WHERE user_id = ?",
                      (json.dumps(bot), _now(), user_id))


# ---------- портфель ----------

def holdings(user_id: int) -> list[dict]:
    return _all(
        "SELECT symbol, amount, invested FROM holdings WHERE user_id = ? AND amount > 0 ORDER BY symbol",
        (user_id,),
    )


def holding(user_id: int, symbol: str) -> dict | None:
    return _one(
        "SELECT symbol, amount, invested FROM holdings WHERE user_id = ? AND symbol = ?",
        (user_id, symbol),
    )


def trades(user_id: int, limit: int = 15) -> list[dict]:
    return _all(
        "SELECT * FROM trades WHERE user_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (user_id, limit),
    )


def trade_count(user_id: int) -> int:
    return _one("SELECT COUNT(*) AS n FROM trades WHERE user_id = ?", (user_id,))["n"]


def _add_holding(c: _Exec, user_id: int, symbol: str, amount: float, usd: float) -> None:
    c.execute(
        """INSERT INTO holdings (user_id, symbol, amount, invested) VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id, symbol) DO UPDATE SET
               amount = holdings.amount + excluded.amount,
               invested = holdings.invested + excluded.invested""",
        (user_id, symbol, amount, usd),
    )


def _take_holding(c: _Exec, user_id: int, symbol: str, amount: float) -> tuple[float, float] | str:
    """Снимает amount монет. Возвращает (фактическое количество, списанная себестоимость)
    или строку-ошибку. «Всё» с допуском на округление снимает строку целиком."""
    row = c.one("SELECT amount, invested FROM holdings WHERE user_id = ? AND symbol = ?", (user_id, symbol))
    if row is None or row["amount"] <= EPS:
        return f"У тебя нет {symbol}."
    if amount <= 0:
        return "Количество должно быть больше нуля."
    if amount > row["amount"] * (1 + 1e-8):
        return f"Столько нет: у тебя {row['amount']:.{COINS[symbol].decimals}f} {symbol}."
    # Количество ходит через callback_data в 10 значащих цифрах — допуск относительный.
    if amount >= row["amount"] * (1 - 1e-8):
        c.execute("DELETE FROM holdings WHERE user_id = ? AND symbol = ?", (user_id, symbol))
        return row["amount"], row["invested"]
    cost = row["invested"] * (amount / row["amount"])
    c.execute("UPDATE holdings SET amount = amount - ?, invested = invested - ? WHERE user_id = ? AND symbol = ?",
              (amount, cost, user_id, symbol))
    return amount, cost


def _log_trade(c: _Exec, user_id: int, side: str, symbol: str, amount: float, price: float, total: float,
               *, at: float | None = None, to_symbol: str | None = None, got: float | None = None,
               pnl: float | None = None, party: str | None = None, is_bot: bool = False,
               note: str | None = None) -> None:
    ts = at if at is not None else time.time()
    c.execute(
        """INSERT INTO trades (user_id, symbol, side, amount, price, total, created_at, ts,
                               to_symbol, got, pnl, party, is_bot, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (user_id, symbol, side, amount, price, total, _ts_text(ts), ts,
         to_symbol, got, pnl, party, int(is_bot), note),
    )


# ---------- сделки ----------

def buy(user_id: int, symbol: str, usd: float, price: float, *,
        at: float | None = None, is_bot: bool = False, note: str | None = None) -> tuple[bool, str]:
    """Купить монет на `usd` долларов по цене `price`. Возвращает (ok, текст).
    at/is_bot/note — переопределения для автотрейдера (он досчитывает историю задним числом)."""
    if symbol not in COINS:
        return False, "Такой монеты нет."
    if price <= 0:
        return False, "Цена недоступна, попробуй чуть позже."
    if usd < MIN_TRADE_USD:
        return False, f"Минимальная сделка — ${MIN_TRADE_USD:.0f}."
    if usd > MAX_TRADE_USD:
        return False, f"Слишком крупно для песочницы: максимум ${MAX_TRADE_USD:,.0f}."

    with tx() as c:
        user = c.one("SELECT cash FROM users WHERE id = ?", (user_id,))
        if user is None:
            return False, "Профиль не найден. Отправь /start."
        if user["cash"] + EPS < usd:
            return False, f"Не хватает ${usd - user['cash']:,.2f}."
        usd = min(usd, user["cash"])
        amount = usd / price
        c.execute("UPDATE users SET cash = cash - ? WHERE id = ?", (usd, user_id))
        _add_holding(c, user_id, symbol, amount, usd)
        _log_trade(c, user_id, "buy", symbol, amount, price, usd, at=at, is_bot=is_bot, note=note)
    return True, f"Куплено {amount:.{COINS[symbol].decimals}f} {symbol}"


def sell(user_id: int, symbol: str, amount: float, price: float, *,
         at: float | None = None, is_bot: bool = False, note: str | None = None) -> tuple[bool, str]:
    """Продать `amount` монет по цене `price`. Продажа всего остатка чистит строку."""
    if symbol not in COINS:
        return False, "Такой монеты нет."
    if price <= 0:
        return False, "Цена недоступна, попробуй чуть позже."
    if amount <= 0:
        return False, "Количество должно быть больше нуля."

    with tx() as c:
        row = c.one("SELECT amount FROM holdings WHERE user_id = ? AND symbol = ?", (user_id, symbol))
        sell_all = row is not None and amount >= row["amount"] * (1 - 1e-8)
        if row is not None and amount * price < MIN_TRADE_USD and not sell_all:
            return False, f"Минимальная сделка — ${MIN_TRADE_USD:.0f}."
        taken = _take_holding(c, user_id, symbol, amount)
        if isinstance(taken, str):
            return False, taken
        amount, cost_out = taken
        total = amount * price
        c.execute("UPDATE users SET cash = cash + ? WHERE id = ?", (total, user_id))
        _log_trade(c, user_id, "sell", symbol, amount, price, total, at=at, pnl=total - cost_out,
                   is_bot=is_bot, note=note)
    pnl = total - cost_out
    return True, f"Продано {amount:.{COINS[symbol].decimals}f} {symbol} за ${total:,.2f} ({pnl:+,.2f}$)"


def swap(user_id: int, src: str, dst: str, amount: float, price_src: float, price_dst: float) -> tuple[bool, str]:
    """Обмен монета↔монета (или USD) по курсу, без комиссии. Себестоимость переезжает
    в новую монету по долларовой стоимости обмена."""
    if src == dst:
        return False, "Выбери разные активы."
    for s in (src, dst):
        if s != "USD" and s not in COINS:
            return False, "Такой монеты нет."
    if price_src <= 0 or price_dst <= 0:
        return False, "Цена недоступна, попробуй чуть позже."
    usd = amount * price_src
    if usd < MIN_TRADE_USD:
        return False, f"Минимальный обмен — ${MIN_TRADE_USD:.0f}."
    with tx() as c:
        if src == "USD":
            user = c.one("SELECT cash FROM users WHERE id = ?", (user_id,))
            if user is None:
                return False, "Профиль не найден. Отправь /start."
            if usd > user["cash"] + EPS:
                return False, f"Не хватает ${usd - user['cash']:,.2f}."
            usd = min(usd, user["cash"])
            amount = usd
            cost = usd
            c.execute("UPDATE users SET cash = cash - ? WHERE id = ?", (usd, user_id))
        else:
            taken = _take_holding(c, user_id, src, amount)
            if isinstance(taken, str):
                return False, taken
            amount, cost = taken
            usd = amount * price_src
        got = usd / price_dst
        if dst == "USD":
            c.execute("UPDATE users SET cash = cash + ? WHERE id = ?", (usd, user_id))
        else:
            _add_holding(c, user_id, dst, got, usd)
        _log_trade(c, user_id, "swap", src, amount, price_src, usd, to_symbol=dst, got=got, pnl=usd - cost)
    return True, f"Обменяно {amount:g} {src} → {got:g} {dst}"


def send(user_id: int, symbol: str, amount: float, to: str, price: float) -> tuple[bool, str]:
    """Перевод внутри песочницы: средства списываются, получатель условный."""
    to = (to or "").strip()
    if len(to) < 4:
        return False, "Укажи адрес или @username получателя."
    if len(to) > 64:
        return False, "Слишком длинный адрес."
    if symbol != "USD" and symbol not in COINS:
        return False, "Такой монеты нет."
    if price <= 0:
        return False, "Цена недоступна, попробуй чуть позже."
    usd = amount * price
    if usd < MIN_TRADE_USD:
        return False, f"Минимальный перевод — ${MIN_TRADE_USD:.0f}."
    with tx() as c:
        user = c.one("SELECT cash, address FROM users WHERE id = ?", (user_id,))
        if user is None:
            return False, "Профиль не найден. Отправь /start."
        if to == user["address"]:
            return False, "Это твой собственный адрес."
        if symbol == "USD":
            if usd > user["cash"] + EPS:
                return False, f"Не хватает ${usd - user['cash']:,.2f}."
            usd = min(usd, user["cash"])
            amount = usd
            c.execute("UPDATE users SET cash = cash - ? WHERE id = ?", (usd, user_id))
        else:
            taken = _take_holding(c, user_id, symbol, amount)
            if isinstance(taken, str):
                return False, taken
            amount, _ = taken
            usd = amount * price
        _log_trade(c, user_id, "send", symbol, amount, price, usd, party=to)
    return True, f"Отправлено {amount:g} {symbol}"


def topup(user_id: int) -> tuple[bool, str]:
    """Кран: +TOPUP_USD на счёт. Считаем пополнения, чтобы «результат» в настройках был честным."""
    with tx() as c:
        if c.one("SELECT 1 AS x FROM users WHERE id = ?", (user_id,)) is None:
            return False, "Профиль не найден. Отправь /start."
        c.execute("UPDATE users SET cash = cash + ?, topups = topups + 1 WHERE id = ?", (TOPUP_USD, user_id))
        _log_trade(c, user_id, "receive", "USD", TOPUP_USD, 1.0, TOPUP_USD, party="Karman Faucet")
    return True, f"+${TOPUP_USD:,.0f} на счёт"


# ---------- стейкинг TON ----------

def stake_earned(user: dict, now: float | None = None) -> float:
    """Доход по стейкингу считается лениво от stake_since."""
    if not user or not user.get("stake_amount"):
        return 0.0
    years = ((now or time.time()) - user["stake_since"]) / (365.25 * 24 * 3600)
    return user["stake_amount"] * STAKE_APY * max(years, 0.0)


def stake(user_id: int, amount: float, price: float) -> tuple[bool, str]:
    if price <= 0:
        return False, "Цена недоступна, попробуй чуть позже."
    now = time.time()
    with tx() as c:
        user = c.one("SELECT stake_amount, stake_invested, stake_since FROM users WHERE id = ?", (user_id,))
        if user is None:
            return False, "Профиль не найден. Отправь /start."
        taken = _take_holding(c, user_id, "TON", amount)
        if isinstance(taken, str):
            return False, taken
        amount, cost = taken
        # Накопленный доход капитализируем и начинаем отсчёт заново.
        total = user["stake_amount"] + stake_earned(user, now) + amount
        c.execute(
            "UPDATE users SET stake_amount = ?, stake_invested = stake_invested + ?, stake_since = ? WHERE id = ?",
            (total, cost, now, user_id),
        )
        _log_trade(c, user_id, "stake", "TON", amount, price, amount * price)
    return True, f"В стейкинге ещё {amount:.3f} TON"


def unstake(user_id: int, price: float) -> tuple[bool, str]:
    now = time.time()
    with tx() as c:
        user = c.one("SELECT stake_amount, stake_invested, stake_since FROM users WHERE id = ?", (user_id,))
        if user is None:
            return False, "Профиль не найден. Отправь /start."
        if user["stake_amount"] <= EPS:
            return False, "Ничего не застейкано."
        earned = stake_earned(user, now)
        total = user["stake_amount"] + earned
        _add_holding(c, user_id, "TON", total, user["stake_invested"])
        c.execute("UPDATE users SET stake_amount = 0, stake_invested = 0, stake_since = 0 WHERE id = ?", (user_id,))
        _log_trade(c, user_id, "unstake", "TON", total, price, total * price, got=earned)
    return True, "TON возвращены в кошелёк"


# ---------- автотрейдер ----------

def get_bot(user_id: int) -> dict:
    """Состояние автотрейдера — JSON-документ (настройки, партии, журнал, статистика).
    Структура описана в trader.BOT_DEFAULT; здесь только хранение."""
    row = _one("SELECT data FROM bot_state WHERE user_id = ?", (user_id,))
    return json.loads(row["data"]) if row else {}


def save_bot(user_id: int, data: dict) -> None:
    with tx() as c:
        c.execute(
            """INSERT INTO bot_state (user_id, data, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET data = excluded.data, updated_at = excluded.updated_at""",
            (user_id, json.dumps(data, ensure_ascii=False), _now()),
        )


def active_bots() -> list[int]:
    """Пользователи, у которых автотрейдер включён. Флаг on лежит внутри JSON —
    выбираем всех и фильтруем в Python: ботов мало, а LIKE по JSON ненадёжен."""
    rows = _all("SELECT user_id, data FROM bot_state")
    return [r["user_id"] for r in rows if json.loads(r["data"]).get("on")]
