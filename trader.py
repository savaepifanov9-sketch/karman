"""Автотрейдер: стратегии, серверный цикл и бэктест.

Раньше жил в браузере (app/index.html) и работал, пока открыто окно. Теперь крутится
в процессе бота для всех, у кого он включён, — приложение только показывает результат.

Устройство то же, что было в JS: стратегия смотрит на ряд цен (почасовые точки за 7 дней
+ текущая) и говорит адаптеру счёта `acct`, что делать. Адаптера два: RealAcct пишет в БД
через db.buy/db.sell, SimAcct считает в памяти для бэктеста; `evaluate` о разнице не знает.
Новую стратегию добавлять в STRATS и в evaluate, больше нигде.

Состояние бота — JSON-документ в db.bot_state (см. BOT_DEFAULT). Партии бота (`lots`)
живут отдельно от holdings: пользователь может продать монеты руками, поэтому продажа
режется по фактическому остатку.

Время везде — unix-секунды (JS работал в миллисекундах; API переводит на границе).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Awaitable, Callable

import db
from coins import COINS
from config import BOT_TICK
from prices import PricesUnavailable, feed

log = logging.getLogger("trader")

# Стратегии; параметры — проценты (кроме maxLots). [ключ, подпись, значение по умолчанию].
STRATS: dict[str, dict] = {
    "dip": {
        "name": "Откат",
        "desc": "Покупает, когда цена падает ниже средней за сутки на заданный процент. "
                "Продаёт по тейк-профиту или стоп-лоссу.",
        "params": [["dip", "Просадка для входа, %", 2.5], ["tp", "Тейк-профит, %", 3], ["sl", "Стоп-лосс, %", 4]],
    },
    "trend": {
        "name": "Тренд",
        "desc": "Покупает, когда быстрая средняя (6ч) пересекает медленную (24ч) снизу вверх. "
                "Продаёт при обратном пересечении или стоп-лоссе.",
        "params": [["sl", "Стоп-лосс, %", 5]],
    },
    "grid": {
        "name": "Сетка",
        "desc": "Покупает партию на каждом шаге падения и продаёт каждую партию, "
                "как только она даёт заданный процент.",
        "params": [["step", "Шаг сетки, %", 2], ["maxLots", "Партий на монету, шт", 4]],
    },
}

BOT_DEFAULT: dict = {
    "on": False, "strategy": "dip", "budget": 3000.0, "perTrade": 300.0,
    "coins": ["TON", "BTC", "ETH", "SOL"],
    "params": {"dip": 2.5, "tp": 3.0, "sl": 4.0, "step": 2.0, "maxLots": 4},
    "lastTick": 0.0, "spent": 0.0, "lots": {}, "log": [], "last": {},
    "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
}

LOG_LIMIT = 60
HISTORY_DAYS = 7      # столько истории бот досчитывает, если давно не проверял
BACKTEST_DAYS = 30


def with_defaults(raw: dict | None) -> dict:
    """Документ из БД + недостающие поля (бот появился позже кошелька, поля добавлялись)."""
    bot = copy.deepcopy(BOT_DEFAULT)
    for key, value in (raw or {}).items():
        if key in ("params", "stats") and isinstance(value, dict):
            bot[key].update(value)
        else:
            bot[key] = value
    return bot


def load_bot(user_id: int) -> dict:
    return with_defaults(db.get_bot(user_id))


def apply_settings(bot: dict, patch: dict) -> str | None:
    """Правит настройки из формы. Возвращает текст ошибки или None."""
    if "strategy" in patch:
        if patch["strategy"] not in STRATS:
            return "Нет такой стратегии."
        bot["strategy"] = patch["strategy"]
    for key in ("budget", "perTrade"):
        if key in patch:
            try:
                v = float(patch[key])
            except (TypeError, ValueError):
                return "Нужно число."
            if not v >= db.MIN_TRADE_USD:
                return f"Минимум ${db.MIN_TRADE_USD:.0f}."
            bot[key] = v
    for key, value in (patch.get("params") or {}).items():
        if key not in BOT_DEFAULT["params"]:
            continue
        try:
            v = float(value)
        except (TypeError, ValueError):
            return "Нужно число."
        if not v > 0:
            return "Параметры должны быть больше нуля."
        bot["params"][key] = int(v) if key == "maxLots" else v
    if "coins" in patch:
        coins = [s for s in patch["coins"] if s in COINS]
        if not coins:
            return "Выбери хотя бы одну монету."
        bot["coins"] = list(dict.fromkeys(coins))
    if bot["perTrade"] > bot["budget"]:
        bot["perTrade"] = bot["budget"]
    return None


# ---------- стратегии ----------

def ma(series: list[float], n: int) -> float:
    tail = series[-n:]
    return sum(tail) / len(tail)


def evaluate(bot: dict, acct, sym: str, series: list[float], p: float, t: float) -> None:
    """Один шаг стратегии для монеты sym в момент t при цене p и истории series (включая p)."""
    P = bot["params"]
    lots = acct.lots.get(sym) or []
    strategy = bot["strategy"]

    if strategy == "dip":
        if not lots:
            if len(series) < 24:
                return
            m = ma(series, 24)
            drop = (m - p) / m * 100
            if drop >= P["dip"]:
                acct.buy(sym, bot["perTrade"], p, t, f"цена на {drop:.1f}% ниже средней за сутки")
        else:
            lot = lots[0]
            ch = (p - lot["entry"]) / lot["entry"] * 100
            if ch >= P["tp"]:
                acct.sell(sym, lot, p, t, f"тейк-профит +{ch:.1f}%")
            elif ch <= -P["sl"]:
                acct.sell(sym, lot, p, t, f"стоп-лосс {ch:.1f}%")

    elif strategy == "trend":
        if len(series) < 25:
            return
        prev = series[:-1]
        fast, slow, pf, ps = ma(series, 6), ma(series, 24), ma(prev, 6), ma(prev, 24)
        if not lots:
            if fast > slow and pf <= ps:
                acct.buy(sym, bot["perTrade"], p, t, "быстрая средняя пересекла медленную вверх")
        else:
            lot = lots[0]
            ch = (p - lot["entry"]) / lot["entry"] * 100
            if fast < slow and pf >= ps:
                acct.sell(sym, lot, p, t, f"тренд развернулся ({'+' if ch >= 0 else ''}{ch:.1f}%)")
            elif ch <= -P["sl"]:
                acct.sell(sym, lot, p, t, f"стоп-лосс {ch:.1f}%")

    elif strategy == "grid":
        for lot in list(lots):
            ch = (p - lot["entry"]) / lot["entry"] * 100
            if ch >= P["step"]:
                acct.sell(sym, lot, p, t, f"партия закрыта +{ch:.1f}%")
        open_lots = acct.lots.get(sym) or []
        if len(open_lots) >= P["maxLots"]:
            return
        if not open_lots:
            acct.buy(sym, bot["perTrade"], p, t, "первая партия сетки")
        else:
            lowest = min(l["entry"] for l in open_lots)
            if p <= lowest * (1 - P["step"] / 100):
                acct.buy(sym, bot["perTrade"], p, t, f"шаг сетки вниз −{(lowest - p) / lowest * 100:.1f}%")


def _push_log(bot: dict, entry: dict) -> None:
    bot["log"].insert(0, entry)
    del bot["log"][LOG_LIMIT:]


class RealAcct:
    """Адаптер над настоящим демо-счётом в БД. Меняет документ bot на месте;
    сохранить его — забота вызывающего (tick_user)."""

    def __init__(self, user_id: int, bot: dict):
        self.user_id, self.bot = user_id, bot
        self.lots: dict[str, list[dict]] = bot["lots"]
        self.events: list[dict] = []   # что произошло за этот проход — для уведомлений

    def buy(self, sym: str, usd: float, p: float, t: float, reason: str) -> None:
        bot = self.bot
        if bot["spent"] + usd > bot["budget"] + 1e-9:
            return
        user = db.get_user(self.user_id)
        if user is None:
            return
        usd = min(usd, user["cash"])
        if usd < db.MIN_TRADE_USD:
            return
        ok, _ = db.buy(self.user_id, sym, usd, p, at=t, is_bot=True, note=reason)
        if not ok:
            return
        self.lots.setdefault(sym, []).append({"amount": usd / p, "entry": p, "invested": usd, "at": t})
        bot["spent"] += usd
        entry = {"at": t, "side": "buy", "s": sym, "usd": usd, "price": p, "reason": reason}
        _push_log(bot, entry)
        self.events.append(entry)

    def sell(self, sym: str, lot: dict, p: float, t: float, reason: str) -> None:
        bot = self.bot
        held = db.holding(self.user_id, sym)
        amount = min(lot["amount"], held["amount"] if held else 0.0)   # пользователь мог продать часть руками
        lst = self.lots.get(sym) or []
        if lot in lst:
            lst.remove(lot)
        if not lst:
            self.lots.pop(sym, None)
        bot["spent"] = max(0.0, bot["spent"] - lot["invested"])
        if amount <= 0:
            return
        ok, _ = db.sell(self.user_id, sym, amount, p, at=t, is_bot=True, note=reason)
        if not ok:
            return
        pnl = amount * p - lot["invested"] * (amount / lot["amount"])
        st = bot["stats"]
        st["trades"] += 1
        if pnl > 0:
            st["wins"] += 1
        st["pnl"] += pnl
        entry = {"at": t, "side": "sell", "s": sym, "usd": amount * p, "price": p, "reason": reason, "pnl": pnl}
        _push_log(bot, entry)
        self.events.append(entry)


class SimAcct:
    """Симуляция для бэктеста: свой кэш и партии, БД не трогает."""

    def __init__(self, bot: dict):
        self.budget = bot["budget"]
        self.cash = bot["budget"]
        self.spent = 0.0
        self.lots: dict[str, list[dict]] = {}
        self.trades = self.wins = 0
        self.pnl = 0.0
        self.log: list[dict] = []

    def buy(self, sym: str, usd: float, p: float, t: float, reason: str) -> None:
        if self.spent + usd > self.budget + 1e-9 or usd > self.cash:
            return
        self.cash -= usd
        self.spent += usd
        self.lots.setdefault(sym, []).append({"amount": usd / p, "entry": p, "invested": usd, "at": t})
        self.log.append({"at": t, "side": "buy", "s": sym, "usd": usd, "price": p, "reason": reason})

    def sell(self, sym: str, lot: dict, p: float, t: float, reason: str) -> None:
        lst = self.lots[sym]
        lst.remove(lot)
        if not lst:
            del self.lots[sym]
        got = lot["amount"] * p
        pnl = got - lot["invested"]
        self.cash += got
        self.spent -= lot["invested"]
        self.trades += 1
        if pnl > 0:
            self.wins += 1
        self.pnl += pnl
        self.log.append({"at": t, "side": "sell", "s": sym, "usd": got, "price": p, "reason": reason, "pnl": pnl})


# ---------- серверный цикл ----------

async def tick_user(user_id: int, bot: dict | None = None) -> list[dict]:
    """Один проход по монетам бота: точки после bot.last[sym] (+ текущая цена) → стратегия.
    Возвращает события (сделки) этого прохода. Сохраняет документ бота."""
    bot = bot or load_bot(user_id)
    if not bot["on"]:
        return []
    try:
        board = await feed.board()
    except PricesUnavailable:
        return []
    acct = RealAcct(user_id, bot)
    now = time.time()
    last = bot.setdefault("last", {})
    for sym in bot["coins"]:
        q = board.quotes.get(sym)
        if q is None:
            continue
        since = last.get(sym) or bot.get("lastTick") or 0
        try:
            pts = await feed.history(sym, HISTORY_DAYS)
        except PricesUnavailable:
            continue   # у этой монеты история не загрузилась — досчитаем в следующий раз
        series = [p for _, p in pts] + [q.usd]
        times = [t for t, _ in pts] + [now]
        for i, t in enumerate(times):
            if t <= since:
                continue
            evaluate(bot, acct, sym, series[: i + 1], series[i], t)
        last[sym] = now
    bot["lastTick"] = now
    db.save_bot(user_id, bot)
    return acct.events


def set_enabled(user_id: int, on: bool) -> dict:
    bot = load_bot(user_id)
    bot["on"] = on
    if on:
        bot["lastTick"] = time.time()   # с включения, а не с прошлого раза: старые сигналы не отыгрываем
        bot["last"] = {}
    db.save_bot(user_id, bot)
    return bot


def close_all(user_id: int, prices: dict[str, float]) -> dict:
    """Продать все партии бота по текущим ценам (кнопка «Закрыть все партии»)."""
    bot = load_bot(user_id)
    acct = RealAcct(user_id, bot)
    now = time.time()
    for sym, lots in list(bot["lots"].items()):
        for lot in list(lots):
            if prices.get(sym):
                acct.sell(sym, lot, prices[sym], now, "закрыто вручную")
    db.save_bot(user_id, bot)
    return bot


def open_pnl(bot: dict, prices: dict[str, float]) -> float:
    return sum(l["amount"] * prices.get(sym, 0.0) - l["invested"]
               for sym, lots in bot["lots"].items() for l in lots)


Notifier = Callable[[int, list[dict]], Awaitable[None]]


async def run_forever(notify: Notifier | None = None) -> None:
    """Фоновая задача: раз в BOT_TICK секунд проходит по всем включённым ботам.
    Ошибка одного пользователя не роняет остальных; notify получает сделки прохода."""
    await asyncio.sleep(5)   # дать боту стартовать и прогреть цены
    while True:
        try:
            for user_id in db.active_bots():
                try:
                    events = await tick_user(user_id)
                except Exception:  # noqa: BLE001
                    log.exception("Автотрейдер %s: проход не удался", user_id)
                    continue
                if events and notify:
                    try:
                        await notify(user_id, events)
                    except Exception as e:  # noqa: BLE001
                        log.warning("Уведомление %s не ушло: %s", user_id, e)
        except Exception:  # noqa: BLE001
            log.exception("Автотрейдер: цикл упал, продолжаю")
        await asyncio.sleep(BOT_TICK)


# ---------- бэктест ----------

async def backtest(bot: dict) -> dict | None:
    """30 дней почасовых точек по всем монетам бота, единая лента по времени.
    Сравнение — те же деньги поровну в те же монеты на весь срок."""
    acct = SimAcct(bot)
    events: list[tuple[float, str, float, int]] = []
    hist: dict[str, list[float]] = {}
    for sym in bot["coins"]:
        try:
            pts = await feed.history(sym, BACKTEST_DAYS)
        except PricesUnavailable:
            continue
        hist[sym] = [p for _, p in pts]
        events.extend((t, sym, p, i) for i, (t, p) in enumerate(pts))
    if not events:
        return None
    events.sort(key=lambda e: e[0])
    for t, sym, p, i in events:
        evaluate(bot, acct, sym, hist[sym][: i + 1], p, t)
    # незакрытые партии оцениваем по последней цене
    open_ = sum(l["amount"] * hist[sym][-1] - l["invested"] for sym, lots in acct.lots.items() for l in lots)
    coins = [s for s in bot["coins"] if s in hist]
    held = sum(bot["budget"] / len(bot["coins"]) * (hist[s][-1] / hist[s][0] - 1) for s in coins)
    return {
        "pnl": acct.pnl + open_, "realized": acct.pnl, "open": open_,
        "trades": acct.trades, "wins": acct.wins, "held": held,
        "log": list(reversed(acct.log[-8:])),
        "from": events[0][0], "to": events[-1][0],
    }
