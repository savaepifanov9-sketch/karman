"""Точка входа Karman. Запуск: .venv\\Scripts\\python.exe bot.py

Один процесс держит три вещи: Telegram-бота (long polling), HTTP-сервер с API и Mini App
(api.py) и фоновый цикл автотрейдера (trader.run_forever). Так его можно разворачивать
одним сервисом на любом хостинге; порт берётся из PORT.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BotCommand, MenuButtonDefault, MenuButtonWebApp, WebAppInfo

import aiohttp

import api
import db
import trader
import ui
from config import APP_NAME, APP_PORT, BOT_TOKEN, KEEPALIVE_EVERY, KEEPALIVE_URL, webapp_url
from handlers import common, market, trade, wallet
from prices import feed

log = logging.getLogger("bot")


async def keepalive() -> None:
    """Сам себя будит: бесплатный Render усыпляет сервис через 15 минут без запросов,
    а вместе с ним засыпают и бот, и автотрейдер. Дублируется внешним пингом (UptimeRobot)."""
    if not KEEPALIVE_URL:
        return
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        while True:
            await asyncio.sleep(KEEPALIVE_EVERY)
            try:
                async with session.get(KEEPALIVE_URL) as r:
                    log.debug("keepalive %s → %s", KEEPALIVE_URL, r.status)
            except Exception as e:  # noqa: BLE001
                log.warning("keepalive не удался: %s", e)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # Порядок значим: кнопки меню (wallet, market) стоят раньше trade, чтобы тап по меню
    # выходил из торгового диалога, а fallback — последним, иначе он съест ввод в FSM.
    dp.include_routers(
        common.router,
        wallet.router,
        market.router,
        trade.router,
        common.fallback_router,
    )
    return dp


def make_notifier(bot: Bot):
    """Сообщение в чат о сделках автотрейдера. Пользователь мог заблокировать бота —
    ошибка логируется и глотается, сделка уже совершена."""

    async def notify(user_id: int, events: list[dict]) -> None:
        user = db.get_user(user_id)
        if user is None:
            return
        try:
            board = await feed.board()
            fx = board.fx
        except Exception:  # noqa: BLE001
            fx = {"usd": 1.0}
        try:
            await bot.send_message(user_id, ui.bot_events_text(events, user["currency"], fx),
                                   reply_markup=ui.wallet_keyboard())
        except TelegramAPIError as e:
            log.warning("Уведомление %s: %s", user_id, e)

    return notify


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN не задан.\n"
            "1. Напиши @BotFather в Telegram, команда /newbot — получишь токен.\n"
            "2. Скопируй .env.example в .env и впиши токен в BOT_TOKEN."
        )

    db.init_db()
    log.info("База: %s", "Postgres" if db.PG else db.DB_PATH)
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()

    me = await bot.get_me()
    log.info("%s запущен как @%s", APP_NAME, me.username)
    await bot.set_my_commands([
        BotCommand(command="start", description="Главный экран"),
        BotCommand(command="app", description="Открыть приложение"),
        BotCommand(command="wallet", description="Кошелёк"),
        BotCommand(command="market", description="Рынок и графики"),
        BotCommand(command="history", description="История сделок"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="cancel", description="Отменить ввод"),
    ])

    # Кнопка «меню» слева от поля ввода открывает Mini App, если адрес известен.
    url = webapp_url()
    if url:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Karman", web_app=WebAppInfo(url=url)))
        log.info("Mini App: %s", url)
    else:
        await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
        log.warning("WEBAPP_URL не задан — кнопки приложения в чате не будет. "
                    "Локально приложение всё равно открывается: http://localhost:%s", APP_PORT)

    # Прогреваем цены, чтобы первый экран не ждал сеть.
    try:
        await feed.board()
    except Exception as e:  # noqa: BLE001
        log.warning("Цены при старте не загрузились: %s", e)

    runner = await api.start_server(APP_PORT)
    background = [
        asyncio.create_task(trader.run_forever(make_notifier(bot)), name="trader"),
        asyncio.create_task(keepalive(), name="keepalive"),
    ]

    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        await runner.cleanup()
        await feed.close()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлен вручную")
