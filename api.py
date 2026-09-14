"""HTTP API для Mini App и раздача самого приложения (app/index.html).

Живёт в том же процессе, что и бот (см. bot.py), на aiohttp — он уже есть у aiogram.
Каждый ответ на действие возвращает полный снимок счёта (`state`): приложение не держит
своей копии данных и просто перерисовывается. Ошибки — 400 с `{"error": "текст"}`,
текст показывается пользователю как есть.

Кто пользователь — решает webauth (initData из Telegram или токен из ссылки). В режиме
разработки (DEV_USER_ID) запрос без заголовка считается этим пользователем — так
приложение открывается в обычном браузере с localhost.

Время в JSON — миллисекунды (как в JS), в БД и trader — секунды. Переводим здесь.
"""

from __future__ import annotations

import copy
import json
import logging
import time
from pathlib import Path

from aiohttp import web

import db
import trader
import webauth
from coins import COINS
from config import APP_NAME, BASE_DIR, DEV_USER_ID, START_CASH
from prices import CHART_PERIODS, Board, PricesUnavailable, feed

log = logging.getLogger("api")

APP_DIR = BASE_DIR / "app"
CURRENCIES = ("usd", "rub", "eur")
# Ключ, под которым middleware кладёт id пользователя в request (типизированный, если aiohttp умеет).
USER = web.RequestKey("user_id", int) if hasattr(web, "RequestKey") else "user_id"


class ApiError(Exception):
    def __init__(self, text: str, status: int = 400):
        super().__init__(text)
        self.text, self.status = text, status


# ---------- снимок состояния ----------

def _ms(ts: float | None) -> int | None:
    return None if ts is None else int(ts * 1000)


def board_view(board: Board | None) -> dict | None:
    if board is None:
        return None
    return {
        "quotes": {s: {"usd": q.usd, "ch": q.change_24h} for s, q in board.quotes.items()},
        "fx": board.fx,
        "at": _ms(board.fetched_at),
    }


def _trade_view(t: dict) -> dict:
    return {
        "id": t["id"], "side": t["side"], "s": t["symbol"], "amount": t["amount"], "price": t["price"],
        "total": t["total"], "at": _ms(t["ts"]), "to": t.get("to_symbol"), "got": t.get("got"),
        "pnl": t.get("pnl"), "party": t.get("party"), "bot": bool(t.get("is_bot")), "note": t.get("note"),
    }


def _bot_view(bot: dict) -> dict:
    view = copy.deepcopy(bot)
    view["lastTick"] = _ms(bot.get("lastTick") or 0)
    view.pop("last", None)
    for lots in view["lots"].values():
        for lot in lots:
            lot["at"] = _ms(lot["at"])
    for e in view["log"]:
        e["at"] = _ms(e["at"])
    return view


def build_state(user_id: int, board: Board | None) -> dict:
    user = db.get_user(user_id)
    if user is None:
        raise ApiError("Профиль не найден. Отправь боту /start.", 404)
    stake = None
    if user["stake_amount"] > 0:
        stake = {"amount": user["stake_amount"], "invested": user["stake_invested"],
                 "since": _ms(user["stake_since"]), "earned": db.stake_earned(user)}
    return {
        "user": {
            "id": user["id"], "first_name": user["first_name"], "cash": user["cash"],
            "currency": user["currency"], "days": user["chart_days"], "address": user["address"],
            "topups": user["topups"], "created": user["created_at"],
        },
        "holdings": {h["symbol"]: {"amount": h["amount"], "invested": h["invested"]} for h in db.holdings(user_id)},
        "stake": stake,
        "trades": [_trade_view(t) for t in db.trades(user_id, 60)],
        "trade_count": db.trade_count(user_id),
        "bot": _bot_view(trader.load_bot(user_id)),
        "board": board_view(board),
        "config": {
            "app": APP_NAME, "start_cash": START_CASH, "topup": db.TOPUP_USD, "apy": db.STAKE_APY,
            "min_trade": db.MIN_TRADE_USD, "strats": trader.STRATS, "periods": list(CHART_PERIODS),
        },
    }


# ---------- вспомогательное ----------

async def _board_or_none(force: bool = False) -> Board | None:
    try:
        return await feed.board(force=force)
    except PricesUnavailable:
        return None


async def _board_required() -> Board:
    board = await _board_or_none()
    if board is None:
        raise ApiError("Биржа не отвечает, попробуй через минуту.", 503)
    return board


def _price(board: Board, sym: str) -> float:
    if sym == "USD":
        return 1.0
    q = board.quotes.get(sym)
    if q is None:
        raise ApiError("Нет цены для этой монеты.")
    return q.usd


def _number(value, name: str = "Сумма") -> float:
    try:
        v = float(str(value).replace(",", ".").replace(" ", ""))
    except (TypeError, ValueError):
        raise ApiError("Введи число больше нуля.") from None
    if not v > 0 or v != v or v in (float("inf"), float("-inf")):
        raise ApiError("Введи число больше нуля.")
    return v


def _symbol(value, allow_usd: bool = False) -> str:
    sym = str(value or "").upper()
    if sym == "USD" and allow_usd:
        return sym
    if sym not in COINS:
        raise ApiError("Такой монеты нет.")
    return sym


async def _json(request: web.Request) -> dict:
    if request.content_length in (None, 0):
        return {}
    try:
        data = await request.json()
    except json.JSONDecodeError:
        raise ApiError("Некорректный запрос.") from None
    return data if isinstance(data, dict) else {}


def _ok(text: str | None, state: dict) -> web.Response:
    return web.json_response({"ok": True, "message": text, "state": state})


def _check(result: tuple[bool, str]) -> str:
    ok, text = result
    if not ok:
        raise ApiError(text)
    return text


# ---------- middleware: ошибки, CORS, пользователь ----------

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
}


@web.middleware
async def middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(headers=CORS)
    try:
        if request.path.startswith("/api/"):
            request[USER] = _resolve_user(request)
        response = await handler(request)
    except ApiError as e:
        response = web.json_response({"ok": False, "error": e.text}, status=e.status)
    except web.HTTPException:
        raise
    except Exception:  # noqa: BLE001
        log.exception("%s %s", request.method, request.path)
        response = web.json_response({"ok": False, "error": "Что-то сломалось на сервере."}, status=500)
    response.headers.update(CORS)
    return response


def _resolve_user(request: web.Request) -> int:
    wu = webauth.authenticate(request.headers.get("Authorization"))
    if wu is None and DEV_USER_ID:
        wu = webauth.WebUser(DEV_USER_ID, "Dev", "dev")
    if wu is None:
        raise ApiError("Открой приложение из Telegram — иначе я не знаю, чей это счёт.", 401)
    user = db.get_user(wu.id)
    if user is None or (wu.first_name and wu.first_name != user["first_name"]):
        db.ensure_user(wu.id, wu.username, wu.first_name)
    return wu.id


# ---------- маршруты ----------

async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "app": APP_NAME, "ts": int(time.time())})


async def index(_: web.Request) -> web.Response:
    # Telegram и браузер агрессивно кэшируют; после правок index.html это мешает.
    return web.FileResponse(APP_DIR / "index.html", headers={"Cache-Control": "no-store"})


async def state(request: web.Request) -> web.Response:
    force = request.query.get("force") == "1"
    return _ok(None, build_state(request[USER], await _board_or_none(force)))


async def board(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "board": board_view(await _board_or_none())})


async def chart(request: web.Request) -> web.Response:
    sym = _symbol(request.match_info["sym"])
    try:
        days = int(request.match_info["days"])
    except ValueError:
        days = 0
    if days not in CHART_PERIODS:
        raise ApiError("Нет такого периода.")
    try:
        pts = await feed.history(sym, days)
    except PricesUnavailable:
        raise ApiError("График недоступен, попробуй позже.", 503) from None
    return web.json_response({"ok": True, "points": [[_ms(t), p] for t, p in pts]})


async def trade(request: web.Request) -> web.Response:
    uid, body = request[USER], await _json(request)
    sym, value = _symbol(body.get("sym")), _number(body.get("value"))
    board = await _board_required()
    price = _price(board, sym)
    if body.get("side") == "buy":
        text = _check(db.buy(uid, sym, value, price))
    elif body.get("side") == "sell":
        text = _check(db.sell(uid, sym, value, price))
    else:
        raise ApiError("Неизвестная операция.")
    return _ok(text, build_state(uid, board))


async def swap(request: web.Request) -> web.Response:
    uid, body = request[USER], await _json(request)
    src, dst = _symbol(body.get("from"), True), _symbol(body.get("to"), True)
    amount = _number(body.get("amount"))
    board = await _board_required()
    text = _check(db.swap(uid, src, dst, amount, _price(board, src), _price(board, dst)))
    return _ok(text, build_state(uid, board))


async def send(request: web.Request) -> web.Response:
    uid, body = request[USER], await _json(request)
    sym, amount = _symbol(body.get("sym"), True), _number(body.get("amount"))
    board = await _board_required()
    text = _check(db.send(uid, sym, amount, str(body.get("to") or ""), _price(board, sym)))
    return _ok(text, build_state(uid, board))


async def topup(request: web.Request) -> web.Response:
    uid = request[USER]
    text = _check(db.topup(uid))
    return _ok(text, build_state(uid, await _board_or_none()))


async def stake(request: web.Request) -> web.Response:
    uid, body = request[USER], await _json(request)
    amount = _number(body.get("amount"))
    board = await _board_required()
    text = _check(db.stake(uid, amount, _price(board, "TON")))
    return _ok(text, build_state(uid, board))


async def unstake(request: web.Request) -> web.Response:
    uid = request[USER]
    board = await _board_required()
    text = _check(db.unstake(uid, _price(board, "TON")))
    return _ok(text, build_state(uid, board))


async def settings(request: web.Request) -> web.Response:
    uid, body = request[USER], await _json(request)
    if "currency" in body:
        if body["currency"] not in CURRENCIES:
            raise ApiError("Нет такой валюты.")
        db.set_currency(uid, body["currency"])
    if "days" in body:
        try:
            days = int(body["days"])
        except (TypeError, ValueError):
            days = 0
        if days not in CHART_PERIODS:
            raise ApiError("Нет такого периода.")
        db.set_chart_days(uid, days)
    return _ok(None, build_state(uid, await _board_or_none()))


async def reset(request: web.Request) -> web.Response:
    uid = request[USER]
    db.reset_account(uid)
    return _ok("Счёт обнулён", build_state(uid, await _board_or_none()))


async def bot_settings(request: web.Request) -> web.Response:
    uid, body = request[USER], await _json(request)
    bot = trader.load_bot(uid)
    err = trader.apply_settings(bot, body)
    if err:
        raise ApiError(err)
    db.save_bot(uid, bot)
    return _ok(None, build_state(uid, await _board_or_none()))


async def bot_toggle(request: web.Request) -> web.Response:
    uid, body = request[USER], await _json(request)
    on = bool(body.get("on"))
    bot = trader.set_enabled(uid, on)
    events = await trader.tick_user(uid, bot) if on else []
    text = "Бот запущен" if on else "Бот остановлен — открытые партии остаются в кошельке"
    if events:
        text = f"Бот запущен: {len(events)} сделок сразу"
    return _ok(text, build_state(uid, await _board_or_none()))


async def bot_close(request: web.Request) -> web.Response:
    uid = request[USER]
    board = await _board_required()
    trader.close_all(uid, {s: q.usd for s, q in board.quotes.items()})
    return _ok("Партии бота закрыты", build_state(uid, board))


async def bot_backtest(request: web.Request) -> web.Response:
    uid = request[USER]
    result = await trader.backtest(trader.load_bot(uid))
    if result is None:
        raise ApiError("Не удалось загрузить историю. Попробуй позже.", 503)
    result = dict(result, **{"from": _ms(result["from"]), "to": _ms(result["to"])})
    for e in result["log"]:
        e["at"] = _ms(e["at"])
    return web.json_response({"ok": True, "result": result})


async def dev_seed(request: web.Request) -> web.Response:
    """Наполняет счёт примерами для проверки вёрстки. Только в режиме разработки."""
    if not DEV_USER_ID:
        raise ApiError("Не найдено.", 404)
    uid = request[USER]
    board = await _board_required()
    p = lambda s: _price(board, s)  # noqa: E731
    db.reset_account(uid)
    db.buy(uid, "TON", 1500, p("TON"))
    db.buy(uid, "BTC", 2000, p("BTC"))
    db.buy(uid, "ETH", 800, p("ETH"))
    db.stake(uid, 300 / p("TON"), p("TON"))
    db.swap(uid, "ETH", "SOL", 0.1, p("ETH"), p("SOL"))
    db.send(uid, "USD", 120, "@friend", 1.0)
    db.topup(uid)
    bot = trader.load_bot(uid)
    bot.update(on=True, lastTick=time.time() - 6 * 86400, last={})   # бот «работал» 6 дней
    await trader.tick_user(uid, bot)
    return _ok("Счёт наполнен примерами", build_state(uid, board))


def make_app() -> web.Application:
    app = web.Application(middlewares=[middleware])
    app.router.add_get("/health", health)
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/board", board)
    app.router.add_get("/api/chart/{sym}/{days}", chart)
    app.router.add_post("/api/trade", trade)
    app.router.add_post("/api/swap", swap)
    app.router.add_post("/api/send", send)
    app.router.add_post("/api/topup", topup)
    app.router.add_post("/api/stake", stake)
    app.router.add_post("/api/unstake", unstake)
    app.router.add_post("/api/settings", settings)
    app.router.add_post("/api/reset", reset)
    app.router.add_post("/api/bot/settings", bot_settings)
    app.router.add_post("/api/bot/toggle", bot_toggle)
    app.router.add_post("/api/bot/close", bot_close)
    app.router.add_post("/api/bot/backtest", bot_backtest)
    app.router.add_post("/api/dev/seed", dev_seed)
    return app


async def start_server(port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("HTTP: порт %s, приложение %s", port, APP_DIR / "index.html")
    return runner
