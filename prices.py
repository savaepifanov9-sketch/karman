"""Цены с CoinGecko (публичный API, без ключа) с кэшем в памяти.

Учёт внутри бота идёт в долларах. Курсы для отображения в ₽/€ берутся из того же
ответа: у CoinGecko один и тот же FX для всех монет, поэтому rub/usd у биткоина —
это курс рубля к доллару.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import aiohttp

from coins import BY_GECKO_ID, COINS, GECKO_IDS
from config import CHART_TTL, PRICE_TTL

log = logging.getLogger("prices")

API = "https://api.coingecko.com/api/v3"
CURRENCIES = ("usd", "rub", "eur")
CHART_PERIODS = (1, 7, 30, 365)  # дней; CoinGecko отдаёт для них разную детализацию


class PricesUnavailable(RuntimeError):
    """Биржа не ответила, а в кэше пусто."""


@dataclass
class Quote:
    usd: float
    change_24h: float  # проценты


@dataclass
class Board:
    quotes: dict[str, Quote]
    fx: dict[str, float] = field(default_factory=lambda: {"usd": 1.0})
    fetched_at: float = 0.0

    @property
    def age(self) -> int:
        return int(time.time() - self.fetched_at)


def parse_simple_price(payload: dict) -> Board:
    """Разбирает ответ /simple/price в Board. Чистая функция, чтобы тестировать без сети."""
    quotes: dict[str, Quote] = {}
    fx = {"usd": 1.0}
    for gecko_id, row in payload.items():
        coin = BY_GECKO_ID.get(gecko_id)
        if coin is None or "usd" not in row:
            continue
        quotes[coin.symbol] = Quote(float(row["usd"]), float(row.get("usd_24h_change") or 0.0))
        if coin.symbol == "BTC":
            for cur in CURRENCIES:
                if cur in row and row[cur]:
                    fx[cur] = float(row[cur]) / float(row["usd"])
    if not quotes:
        raise ValueError("пустой ответ")
    return Board(quotes, fx, time.time())


# Публичный CoinGecko отдаёт 429 уже после ~10 запросов в минуту с одного IP, а сервер
# обслуживает всех пользователей и автотрейдер сразу. Поэтому: запросы идут по одному
# с паузой, при 429 ждём и повторяем, а долгие графики живут в кэше дольше коротких —
# почасовые точки за неделю за пять минут не меняются.
MIN_GAP = 1.5          # секунд между запросами
RETRIES = 3
CHART_TTLS = {1: CHART_TTL, 7: 30 * 60, 30: 60 * 60, 365: 6 * 3600}


class PriceFeed:
    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._board: Board | None = None
        self._charts: dict[tuple[str, int], tuple[float, list[tuple[float, float]]]] = {}
        self._lock = asyncio.Lock()
        self._gate = asyncio.Lock()      # один запрос к CoinGecko за раз
        self._last_request = 0.0
        self._inflight: dict[tuple[str, int], asyncio.Future] = {}

    async def _get(self, path: str, **params) -> dict | list:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "karman-demo-wallet/1.0"},
            )
        async with self._gate:
            for attempt in range(1, RETRIES + 1):
                gap = MIN_GAP - (time.monotonic() - self._last_request)
                if gap > 0:
                    await asyncio.sleep(gap)
                self._last_request = time.monotonic()
                async with self._session.get(f"{API}{path}", params=params) as r:
                    if r.status != 429:
                        r.raise_for_status()
                        return await r.json()
                    if attempt == RETRIES:
                        raise PricesUnavailable("лимит запросов CoinGecko")
                    try:
                        wait = float(r.headers.get("Retry-After", 0)) or 3.0 * attempt
                    except ValueError:
                        wait = 3.0 * attempt
                    wait = min(wait, 20)
                    log.info("CoinGecko 429, жду %.0f с (%s)", wait, path)
                    await asyncio.sleep(wait)
        raise PricesUnavailable("лимит запросов CoinGecko")  # недостижимо, для типизации

    async def board(self, force: bool = False) -> Board:
        """Актуальные цены. Свежий кэш отдаётся сразу, протухший — обновляется;
        если сеть упала, возвращается старый кэш (сколько бы ему ни было)."""
        if self._board is not None and not force and self._board.age < PRICE_TTL:
            return self._board
        async with self._lock:
            if self._board is not None and not force and self._board.age < PRICE_TTL:
                return self._board
            try:
                payload = await self._get(
                    "/simple/price",
                    ids=",".join(GECKO_IDS),
                    vs_currencies=",".join(CURRENCIES),
                    include_24hr_change="true",
                )
                self._board = parse_simple_price(payload)
            except Exception as e:  # noqa: BLE001 — любая сетевая беда: отдаём кэш
                log.warning("Цены не обновились: %s", e)
                if self._board is None:
                    raise PricesUnavailable(str(e)) from e
            return self._board

    async def history(self, symbol: str, days: int) -> list[tuple[float, float]]:
        """История цены в USD: список (unix-секунды, цена)."""
        key = (symbol, days)
        cached = self._charts.get(key)
        if cached and time.time() - cached[0] < CHART_TTLS.get(days, CHART_TTL):
            return cached[1]
        # Один и тот же график могут запросить сразу несколько корутин (бот и пользователи) —
        # в сеть идёт первая, остальные ждут её результат.
        pending = self._inflight.get(key)
        if pending is not None:
            return await asyncio.shield(pending)
        fut = self._inflight[key] = asyncio.get_running_loop().create_future()
        try:
            payload = await self._get(
                f"/coins/{COINS[symbol].gecko_id}/market_chart",
                vs_currency="usd",
                days=str(days),
            )
            points = [(ts / 1000, float(p)) for ts, p in payload["prices"]]
        except Exception as e:  # noqa: BLE001
            log.warning("График %s/%s не загрузился: %s", symbol, days, e)
            self._inflight.pop(key, None)
            if cached:
                fut.set_result(cached[1])
                return cached[1]
            fut.set_exception(PricesUnavailable(str(e)))
            fut.exception()   # помечаем «прочитано», иначе asyncio ругается, если ждущих не было
            raise PricesUnavailable(str(e)) from e
        self._charts[key] = (time.time(), points)
        self._inflight.pop(key, None)
        fut.set_result(points)
        return points

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


feed = PriceFeed()
