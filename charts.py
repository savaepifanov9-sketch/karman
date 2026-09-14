"""Рендер графика цены в PNG. Только matplotlib и чистые данные — сюда не ходят в сеть и БД."""

from __future__ import annotations

import io
from datetime import datetime

import matplotlib

matplotlib.use("Agg")  # без окна: рисуем в память

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402

from coins import COINS  # noqa: E402

# Палитра в духе тёмной темы кошелька.
BG = "#10161F"
PANEL = "#1B2331"
TEXT = "#E8EDF5"
MUTED = "#7C8797"
UP = "#39D98A"
DOWN = "#FF5C5C"
ACCENT = "#45AEF5"

PERIOD_LABEL = {1: "24 часа", 7: "7 дней", 30: "30 дней", 365: "год"}
SYMBOLS = {"usd": "$", "rub": "₽", "eur": "€"}


def render_chart(symbol: str, points: list[tuple[float, float]], days: int,
                 currency: str = "usd", rate: float = 1.0) -> bytes:
    """points — (unix-секунды, цена в USD). Возвращает PNG."""
    coin = COINS[symbol]
    xs = [datetime.fromtimestamp(ts) for ts, _ in points]
    ys = [p * rate for _, p in points]
    first, last = ys[0], ys[-1]
    change = (last - first) / first * 100 if first else 0.0
    color = UP if change >= 0 else DOWN
    cur = SYMBOLS.get(currency, "$")

    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=140)
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)

    ax.plot(xs, ys, color=color, linewidth=2.2)
    ax.fill_between(xs, ys, min(ys), color=color, alpha=0.12)

    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(PANEL)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.yaxis.grid(True, color=PANEL, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.yaxis.tick_right()
    ax.yaxis.set_major_formatter(lambda v, _: f"{cur}{_fmt(v)}")

    if days == 1:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    elif days <= 30:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    else:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%m.%Y"))
    ax.margins(x=0.01, y=0.15)

    sign = "+" if change >= 0 else ""
    # Эмодзи в заголовок не ставим: в шрифтах matplotlib их нет, получится пустой квадрат.
    fig.text(0.05, 0.93, f"{coin.name} · {symbol}", color=TEXT, fontsize=15,
             fontweight="bold", va="top")
    fig.text(0.05, 0.85, f"{cur}{_fmt(last)}", color=TEXT, fontsize=22, fontweight="bold", va="top")
    fig.text(0.05, 0.76, f"{sign}{change:.2f}% за {PERIOD_LABEL.get(days, f'{days} д')}",
             color=color, fontsize=11, va="top")
    fig.text(0.95, 0.93, "Karman · demo", color=MUTED, fontsize=9, ha="right", va="top")

    fig.subplots_adjust(left=0.04, right=0.9, top=0.68, bottom=0.12)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()


def _fmt(value: float) -> str:
    """Цена без лишних нулей: 76 934 → «76 934», 1.3512 → «1.351», 0.00213 → «0.00213»."""
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 1:
        return f"{value:,.3f}".rstrip("0").rstrip(".")
    return f"{value:.5g}"
