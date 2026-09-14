"""Справочник монет. Единственное место, где перечислено, чем можно торговать."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Coin:
    symbol: str      # тикер, он же ключ везде в БД и callback_data
    gecko_id: str    # id на CoinGecko
    name: str
    emoji: str
    decimals: int    # сколько знаков показывать в количестве монет
    tagline: str     # одна строка характера для карточки


COINS: dict[str, Coin] = {
    c.symbol: c
    for c in (
        Coin("TON", "the-open-network", "Toncoin", "💎", 3, "Родная монета Telegram. Бывший Gram."),
        Coin("BTC", "bitcoin", "Bitcoin", "🟠", 6, "Цифровое золото. Старший в комнате."),
        Coin("ETH", "ethereum", "Ethereum", "🔷", 5, "Смарт-контракты и всё, что на них выросло."),
        Coin("SOL", "solana", "Solana", "🟣", 4, "Быстрая, дешёвая, иногда ложится поспать."),
        Coin("BNB", "binancecoin", "BNB", "🟡", 4, "Монета крупнейшей биржи."),
        Coin("DOGE", "dogecoin", "Dogecoin", "🐕", 2, "Начиналась как шутка. Шутка затянулась."),
        Coin("NOT", "notcoin", "Notcoin", "⚡", 0, "Та самая, которую все тапали."),
    )
}

GECKO_IDS = [c.gecko_id for c in COINS.values()]
BY_GECKO_ID = {c.gecko_id: c for c in COINS.values()}
