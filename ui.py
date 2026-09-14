"""Тексты и клавиатуры. Слой представления: считает только для показа, в БД и сеть не ходит.

Все строки от пользователя (имя) проходят через html.escape — parse_mode=HTML включён глобально.
"""

from __future__ import annotations

import html
from datetime import datetime

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import webauth
from coins import COINS, Coin
from config import APP_NAME, STAKE_APY, START_CASH, webapp_url
from prices import CHART_PERIODS, Board, Quote

BTN_WALLET = "👛 Кошелёк"
BTN_MARKET = "📈 Рынок"
BTN_HISTORY = "🧾 История"
BTN_SETTINGS = "⚙️ Настройки"
BTN_APP = "📱 Приложение"

CUR_SIGN = {"usd": "$", "rub": "₽", "eur": "€"}
CUR_NAME = {"usd": "Доллары", "rub": "Рубли", "eur": "Евро"}
PERIOD_SHORT = {1: "24ч", 7: "7д", 30: "30д", 365: "1г"}
NBSP = " "  # узкий неразрывный пробел между разрядами


# ---------- форматирование ----------

def money(usd: float, currency: str = "usd", fx: dict[str, float] | None = None) -> str:
    rate = (fx or {}).get(currency, 1.0)
    value = usd * rate
    sign = "-" if value < 0 else ""
    value = abs(value)
    if currency == "rub":
        body = f"{value:,.0f}".replace(",", NBSP)
        return f"{sign}{body}{NBSP}₽"
    if value >= 10_000:
        body = f"{value:,.0f}".replace(",", NBSP)
    elif value >= 1:
        body = f"{value:,.2f}".replace(",", NBSP)
    else:
        body = f"{value:.4g}"
    return f"{sign}{CUR_SIGN[currency]}{body}"


def amount(symbol: str, qty: float) -> str:
    d = COINS[symbol].decimals
    s = f"{qty:,.{d}f}".replace(",", NBSP)
    return f"{s} {symbol}"


def pct(change: float) -> str:
    arrow = "▲" if change >= 0 else "▼"
    return f"{arrow} {abs(change):.2f}%"


def signed_money(usd: float, currency: str, fx: dict[str, float] | None) -> str:
    return ("+" if usd >= 0 else "−") + money(abs(usd), currency, fx)


# ---------- характер ----------

def greeting(first_name: str | None) -> str:
    hour = datetime.now().hour
    if 5 <= hour < 12:
        word = "Доброе утро"
    elif 12 <= hour < 18:
        word = "Добрый день"
    elif 18 <= hour < 23:
        word = "Добрый вечер"
    else:
        word = "Не спится"
    name = html.escape(first_name or "трейдер")
    return f"{word}, {name}."


def mood_line(pnl_pct: float | None, has_coins: bool) -> str:
    if not has_coins:
        return "Портфель пустой — рынок ждёт первую сделку."
    if pnl_pct is None:
        return ""
    if pnl_pct >= 10:
        return "Портфель цветёт. Не забудь, что это демо 🌱"
    if pnl_pct >= 2:
        return "В плюсе. Спокойно, ровно, как и надо."
    if pnl_pct > -2:
        return "Около нуля. Рынок думает."
    if pnl_pct > -10:
        return "Немного штормит. Дышим 🌊"
    return "Глубокая просадка. Хорошо, что деньги ненастоящие."


# ---------- главное меню ----------

def main_menu() -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_WALLET), KeyboardButton(text=BTN_MARKET)],
        [KeyboardButton(text=BTN_HISTORY), KeyboardButton(text=BTN_SETTINGS)],
    ]
    url = webapp_url()
    if url:
        rows.insert(0, [KeyboardButton(text=BTN_APP, web_app=WebAppInfo(url=url))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def app_button(text: str = "📱 Открыть приложение", anchor: str = "") -> InlineKeyboardButton | None:
    """Инлайн-кнопка Mini App; None, если публичного адреса нет."""
    url = webapp_url()
    if not url:
        return None
    return InlineKeyboardButton(text=text, web_app=WebAppInfo(url=url + anchor))


def app_keyboard(user_id: int) -> InlineKeyboardMarkup | None:
    """Две кнопки: Mini App внутри Telegram и та же страница в обычном браузере —
    запасной вход, если встроенный браузер Telegram не пускает (прокси, блокировки).
    В браузере Telegram не подписывает запросы, поэтому в ссылку зашит токен пользователя."""
    btn = app_button()
    if not btn:
        return None
    browser = InlineKeyboardButton(
        text="🌐 Открыть в браузере", url=f"{webapp_url()}?t={webauth.browser_token(user_id)}"
    )
    return InlineKeyboardMarkup(inline_keyboard=[[btn], [browser]])


def welcome_text(first_name: str | None) -> str:
    return (
        f"{greeting(first_name)}\n\n"
        f"Это <b>{APP_NAME}</b> — карманный крипто-кошелёк-тренажёр.\n"
        f"Цены настоящие, деньги — нет. На счёте {money(START_CASH)} демо-долларов: "
        "покупай TON, BTC, ETH и другие монеты, смотри графики, считай прибыль.\n\n"
        "Ни блокчейна, ни реальных переводов здесь нет — только тренировка."
    )


# ---------- кошелёк ----------

def stake_earned(user, now: float | None = None) -> float:
    """Доход по стейкингу TON, считается лениво от stake_since (та же формула, что в db)."""
    amount = user.get("stake_amount") or 0
    if not amount:
        return 0.0
    years = ((now or datetime.now().timestamp()) - user["stake_since"]) / (365.25 * 24 * 3600)
    return amount * STAKE_APY * max(years, 0.0)


def wallet_text(user, rows, board: Board, bot: dict | None = None) -> str:
    cur, fx = user["currency"], board.fx
    lines = [f"👛 <b>{APP_NAME}</b>", ""]

    coins_value = 0.0
    coins_yesterday = 0.0
    invested = 0.0
    items: list[str] = []
    for h in rows:
        q = board.quotes.get(h["symbol"])
        if q is None:
            continue
        value = h["amount"] * q.usd
        coins_value += value
        coins_yesterday += value / (1 + q.change_24h / 100) if q.change_24h > -100 else value
        invested += h["invested"]
        coin = COINS[h["symbol"]]
        pnl = value - h["invested"]
        items.append(
            f"{coin.emoji} <b>{coin.symbol}</b>  {amount(coin.symbol, h['amount'])}\n"
            f"      {money(value, cur, fx)}  ·  {signed_money(pnl, cur, fx)}"
        )

    staked_ton = (user.get("stake_amount") or 0) + stake_earned(user)
    ton = board.quotes.get("TON")
    staked_value = staked_ton * ton.usd if (staked_ton and ton) else 0.0

    total = user["cash"] + coins_value + staked_value
    lines.append("Общий баланс")
    lines.append(f"<b>{money(total, cur, fx)}</b>")
    if coins_value:
        day = (coins_value - coins_yesterday) / coins_yesterday * 100 if coins_yesterday else 0
        lines.append(f"{pct(day)} за 24ч по монетам")
    lines.append("")
    lines.append(f"💵 Наличные   <b>{money(user['cash'], cur, fx)}</b>")
    if items:
        lines.append("")
        lines.extend(items)
    if staked_ton:
        lines.append("")
        lines.append(f"🔒 В стейкинге  {amount('TON', staked_ton)}  ·  {money(staked_value, cur, fx)}"
                     f"  ·  {STAKE_APY * 100:.1f}% годовых")
    if bot and bot.get("on"):
        st = bot.get("stats") or {}
        lines.append(f"🤖 Автотрейдер работает  ·  {st.get('trades', 0)} сделок  ·  "
                     f"{signed_money(st.get('pnl', 0.0), cur, fx)} по закрытым")

    pnl_pct = (coins_value - invested) / invested * 100 if invested else None
    lines.append("")
    lines.append(f"<i>{mood_line(pnl_pct, bool(items) or bool(staked_ton))}</i>")
    if board.age > 120:
        lines.append(f"<i>Цены обновлены {board.age // 60} мин назад.</i>")
    return "\n".join(lines)


def wallet_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📈 Рынок", callback_data="market")
    kb.button(text="🔄 Обновить", callback_data="wallet")
    kb.button(text="🧾 История", callback_data="hist")
    kb.button(text="⚙️ Настройки", callback_data="settings")
    sizes = [2, 2]
    app = app_button()
    if app:
        kb.add(app)
        sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


# ---------- рынок ----------

def market_text(board: Board) -> str:
    stale = f"\n<i>Цены обновлены {board.age // 60} мин назад.</i>" if board.age > 120 else ""
    return "📈 <b>Рынок</b>\nНажми на монету — откроется график и торговля." + stale


def market_keyboard(board: Board, currency: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for coin in COINS.values():
        q = board.quotes.get(coin.symbol)
        if q is None:
            continue
        kb.button(
            text=f"{coin.emoji} {coin.symbol}   {money(q.usd, currency, board.fx)}   {pct(q.change_24h)}",
            callback_data=f"coin:{coin.symbol}",
        )
    kb.button(text="👛 Кошелёк", callback_data="wallet")
    kb.adjust(1)
    return kb.as_markup()


# ---------- карточка монеты ----------

def coin_caption(coin: Coin, q: Quote, hold, cash: float, currency: str, fx: dict[str, float]) -> str:
    lines = [
        f"{coin.emoji} <b>{coin.name}</b> ({coin.symbol})",
        f"<b>{money(q.usd, currency, fx)}</b>  {pct(q.change_24h)} за 24ч",
        f"<i>{coin.tagline}</i>",
        "",
    ]
    if hold is not None and hold["amount"] > 0:
        value = hold["amount"] * q.usd
        pnl = value - hold["invested"]
        pnl_pct = pnl / hold["invested"] * 100 if hold["invested"] else 0
        lines.append(
            f"У тебя: <b>{amount(coin.symbol, hold['amount'])}</b> = {money(value, currency, fx)}\n"
            f"Прибыль: {signed_money(pnl, currency, fx)} ({pnl_pct:+.1f}%)"
        )
    else:
        lines.append("У тебя этой монеты пока нет.")
    lines.append(f"Свободно: {money(cash, currency, fx)}")
    return "\n".join(lines)


def coin_keyboard(symbol: str, days: int, has_coin: bool) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🟢 Купить", callback_data=f"buy:{symbol}")
    if has_coin:
        kb.button(text="🔴 Продать", callback_data=f"sell:{symbol}")
    for d in CHART_PERIODS:
        label = PERIOD_SHORT[d]
        kb.button(
            text=f"· {label} ·" if d == days else label,
            callback_data="noop" if d == days else f"chart:{symbol}:{d}",
        )
    kb.button(text="← Рынок", callback_data="market")
    sizes = [2 if has_coin else 1, len(CHART_PERIODS), 1]
    app = app_button("📱 Открыть в приложении", f"#coin={symbol}")
    if app:
        kb.add(app)
        sizes.append(1)
    kb.adjust(*sizes)
    return kb.as_markup()


# ---------- торговля ----------

QUICK_USD = (50, 100, 500, 1000)
QUICK_PCT = (25, 50, 100)


def buy_prompt(coin: Coin, q: Quote, cash: float, currency: str, fx: dict[str, float]) -> str:
    return (
        f"🟢 <b>Покупка {coin.symbol}</b> по {money(q.usd, currency, fx)}\n"
        f"Свободно: {money(cash, currency, fx)}\n\n"
        "На какую сумму? Выбери или напиши число в долларах."
    )


def buy_keyboard(symbol: str, cash: float) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    quick = [v for v in QUICK_USD if v <= cash]
    for v in quick:
        kb.button(text=f"${v}", callback_data=f"buyq:{symbol}:{v}")
    for p in QUICK_PCT:
        kb.button(text=f"{p}%" if p < 100 else "Всё", callback_data=f"buyp:{symbol}:{p}")
    kb.button(text="✖ Отмена", callback_data=f"tcancel:{symbol}")
    kb.adjust(max(len(quick), 1), len(QUICK_PCT), 1)
    return kb.as_markup()


def sell_prompt(coin: Coin, q: Quote, hold, currency: str, fx: dict[str, float]) -> str:
    return (
        f"🔴 <b>Продажа {coin.symbol}</b> по {money(q.usd, currency, fx)}\n"
        f"У тебя: {amount(coin.symbol, hold['amount'])} "
        f"= {money(hold['amount'] * q.usd, currency, fx)}\n\n"
        f"Сколько продать? Выбери долю или напиши количество {coin.symbol}."
    )


def sell_keyboard(symbol: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for p in QUICK_PCT:
        kb.button(text=f"{p}%" if p < 100 else "Всё", callback_data=f"sellp:{symbol}:{p}")
    kb.button(text="✖ Отмена", callback_data=f"tcancel:{symbol}")
    kb.adjust(len(QUICK_PCT), 1)
    return kb.as_markup()


def confirm_text(side: str, coin: Coin, qty: float, usd: float, q: Quote,
                 currency: str, fx: dict[str, float]) -> str:
    verb = "Купить" if side == "buy" else "Продать"
    return (
        f"{'🟢' if side == 'buy' else '🔴'} {verb} <b>{amount(coin.symbol, qty)}</b> "
        f"{'за' if side == 'buy' else 'и получить'} <b>{money(usd, currency, fx)}</b>?\n"
        f"Цена: {money(q.usd, currency, fx)} за 1 {coin.symbol}"
    )


def confirm_keyboard(side: str, symbol: str, value: float) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data=f"conf:{side}:{symbol}:{value:.10g}")
    kb.button(text="✖ Отмена", callback_data=f"tcancel:{symbol}")
    kb.adjust(2)
    return kb.as_markup()


def after_trade_keyboard(symbol: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👛 Кошелёк", callback_data="wallet")
    kb.button(text=f"{COINS[symbol].emoji} {symbol}", callback_data=f"coin:{symbol}")
    kb.adjust(2)
    return kb.as_markup()


# ---------- история ----------

def _qty(symbol: str, qty: float) -> str:
    return amount(symbol, qty) if symbol in COINS else f"{qty:,.2f} {symbol}".replace(",", NBSP)


def history_line(t: dict, currency: str, fx: dict[str, float]) -> str:
    """Одна операция из trades: покупки и продажи бота помечены 🤖, остальные типы —
    обмен, перевод, кран, стейкинг — тоже здесь, чтобы история в чате совпадала с приложением."""
    side, sym, when = t["side"], t["symbol"], t["created_at"][5:16].replace("-", ".")
    who = "🤖 " if t.get("is_bot") else ""
    total = money(t["total"], currency, fx)
    if side == "buy":
        body = f"{who}🟢 Купил {_qty(sym, t['amount'])} за {total}"
    elif side == "sell":
        body = f"{who}🔴 Продал {_qty(sym, t['amount'])} за {total} ({signed_money(t.get('pnl') or 0.0, currency, fx)})"
    elif side == "swap":
        body = f"⇄ Обмен {_qty(sym, t['amount'])} → {_qty(t['to_symbol'], t['got'] or 0.0)}"
    elif side == "send":
        body = f"↗ Перевод {_qty(sym, t['amount'])} → {html.escape(t.get('party') or '')}"
    elif side == "receive":
        body = f"↙ Пополнение +{total} от {html.escape(t.get('party') or '')}"
    elif side == "stake":
        body = f"🔒 В стейкинг {_qty(sym, t['amount'])}"
    elif side == "unstake":
        body = f"🔓 Из стейкинга {_qty(sym, t['amount'])} (+{_qty('TON', t.get('got') or 0.0)} дохода)"
    else:
        body = f"• {side} {_qty(sym, t['amount'])}"
    return f"{body}  <i>{when}</i>"


def history_text(rows, currency: str, fx: dict[str, float]) -> str:
    if not rows:
        return "🧾 <b>История</b>\n\nСделок пока нет. Загляни на рынок."
    lines = ["🧾 <b>История</b> — последние операции", ""]
    lines.extend(history_line(t, currency, fx) for t in rows)
    return "\n".join(lines)


def bot_events_text(events: list[dict], currency: str, fx: dict[str, float]) -> str:
    """Уведомление о сделках автотрейдера за один проход."""
    lines = ["🤖 <b>Автотрейдер</b>"]
    for e in events:
        verb = "🟢 Купил" if e["side"] == "buy" else "🔴 Продал"
        line = f"{verb} {e['s']} на {money(e['usd'], currency, fx)} по {money(e['price'], currency, fx)}"
        if e["side"] == "sell":
            line += f" — {signed_money(e.get('pnl') or 0.0, currency, fx)}"
        lines.append(f"{line}\n<i>{html.escape(e['reason'])}</i>")
    return "\n".join(lines)


def history_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="👛 Кошелёк", callback_data="wallet")
    kb.button(text="📈 Рынок", callback_data="market")
    kb.adjust(2)
    return kb.as_markup()


# ---------- настройки ----------

def settings_text(user, trade_count: int) -> str:
    return (
        "⚙️ <b>Настройки</b>\n\n"
        f"Валюта показа: <b>{CUR_NAME[user['currency']]}</b>\n"
        f"Период графика: <b>{PERIOD_SHORT[user['chart_days']]}</b>\n"
        f"Сделок совершено: <b>{trade_count}</b>\n\n"
        "Учёт всегда ведётся в долларах, валюта влияет только на отображение."
    )


def settings_keyboard(user) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cur in ("usd", "rub", "eur"):
        mark = "• " if cur == user["currency"] else ""
        kb.button(text=f"{mark}{CUR_SIGN[cur]} {CUR_NAME[cur]}", callback_data=f"set:cur:{cur}")
    for d in CHART_PERIODS:
        mark = "• " if d == user["chart_days"] else ""
        kb.button(text=f"{mark}{PERIOD_SHORT[d]}", callback_data=f"set:days:{d}")
    kb.button(text="ℹ️ О приложении", callback_data="about")
    kb.button(text="🗑 Сбросить демо-счёт", callback_data="reset")
    kb.button(text="👛 Кошелёк", callback_data="wallet")
    kb.adjust(3, len(CHART_PERIODS), 1, 1, 1)
    return kb.as_markup()


def reset_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="Да, обнулить", callback_data="reset:yes")
    kb.button(text="✖ Отмена", callback_data="settings")
    kb.adjust(2)
    return kb.as_markup()


ABOUT_TEXT = (
    f"ℹ️ <b>{APP_NAME}</b>\n\n"
    "Тренажёр крипто-кошелька в духе Tonkeeper. Котировки — CoinGecko, обновляются раз в минуту, "
    "графики — раз в пять минут.\n\n"
    "Здесь нет блокчейна, кошельков и настоящих денег: демо-счёт живёт в базе бота, "
    "сделки исполняются по текущей цене без комиссии и проскальзывания.\n\n"
    "Команды: /start, /wallet, /market, /history, /settings, /cancel."
)
