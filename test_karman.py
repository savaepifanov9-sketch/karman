"""Тесты Karman. Запуск: .venv\\Scripts\\python.exe -m unittest -v

Временная БД подставляется через DB_PATH до импорта db — поэтому импорты ниже не сверху.
"""

from __future__ import annotations

import os
import tempfile
import unittest

_tmp = tempfile.TemporaryDirectory()
os.environ["DB_PATH"] = os.path.join(_tmp.name, "test.db")
os.environ["START_CASH"] = "1000"
os.environ["WEBAPP_URL"] = "https://example.test/karman/"  # не зависеть от .env разработчика
os.environ["BOT_TOKEN"] = "123456:TEST-TOKEN"                # подписи initData и браузерных токенов
os.environ["DATABASE_URL"] = ""                              # тесты всегда на SQLite
os.environ["DEV_USER_ID"] = "0"

import db  # noqa: E402
import trader  # noqa: E402
import ui  # noqa: E402
import webauth  # noqa: E402
from charts import render_chart  # noqa: E402
from handlers.trade import parse_number  # noqa: E402
from prices import Board, Quote, parse_simple_price  # noqa: E402

U = 100


def fresh_user(user_id: int = U):
    user = db.ensure_user(user_id, "tester", "Тест")
    db.reset_account(user_id)
    db.set_currency(user_id, "usd")
    db.set_chart_days(user_id, 7)
    return user


def tearDownModule():
    db.close()
    _tmp.cleanup()


class AccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        fresh_user()

    def test_new_user_gets_start_cash(self):
        self.assertEqual(db.get_user(U)["cash"], 1000)

    def test_buy_moves_cash_into_holding(self):
        ok, _ = db.buy(U, "TON", 100, 2.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(db.get_user(U)["cash"], 900)
        h = db.holding(U, "TON")
        self.assertAlmostEqual(h["amount"], 50)
        self.assertAlmostEqual(h["invested"], 100)

    def test_buy_more_than_cash_fails(self):
        ok, msg = db.buy(U, "BTC", 5000, 70000)
        self.assertFalse(ok)
        self.assertIn("Не хватает", msg)
        self.assertEqual(db.get_user(U)["cash"], 1000)

    def test_buy_below_minimum_fails(self):
        ok, _ = db.buy(U, "TON", 0.5, 2.0)
        self.assertFalse(ok)

    def test_unknown_coin_fails(self):
        ok, _ = db.buy(U, "XXX", 100, 1.0)
        self.assertFalse(ok)

    def test_sell_part_keeps_proportional_cost(self):
        db.buy(U, "ETH", 400, 2000)          # 0.2 ETH за 400
        ok, _ = db.sell(U, "ETH", 0.1, 3000)  # половину по 3000
        self.assertTrue(ok)
        h = db.holding(U, "ETH")
        self.assertAlmostEqual(h["amount"], 0.1)
        self.assertAlmostEqual(h["invested"], 200)
        self.assertAlmostEqual(db.get_user(U)["cash"], 600 + 300)

    def test_sell_all_removes_holding_even_with_rounding(self):
        db.buy(U, "TON", 100, 1.35)
        exact = db.holding(U, "TON")["amount"]
        rounded = float(f"{exact:.10g}")   # так число ходит через callback_data
        ok, _ = db.sell(U, "TON", rounded, 1.35)
        self.assertTrue(ok)
        self.assertIsNone(db.holding(U, "TON"))
        self.assertAlmostEqual(db.get_user(U)["cash"], 1000, places=6)

    def test_sell_more_than_owned_fails(self):
        db.buy(U, "TON", 100, 2.0)
        ok, msg = db.sell(U, "TON", 60, 2.0)
        self.assertFalse(ok)
        self.assertIn("Столько нет", msg)

    def test_sell_without_holding_fails(self):
        ok, _ = db.sell(U, "DOGE", 1, 0.1)
        self.assertFalse(ok)

    def test_trades_are_recorded_newest_first(self):
        db.buy(U, "TON", 100, 2.0)
        db.sell(U, "TON", 10, 2.5)
        rows = db.trades(U)
        self.assertEqual([r["side"] for r in rows], ["sell", "buy"])
        self.assertEqual(db.trade_count(U), 2)

    def test_reset_restores_everything(self):
        db.buy(U, "BTC", 500, 50000)
        db.reset_account(U)
        self.assertEqual(db.get_user(U)["cash"], 1000)
        self.assertEqual(db.holdings(U), [])
        self.assertEqual(db.trade_count(U), 0)

    def test_settings_persist(self):
        db.set_currency(U, "rub")
        db.set_chart_days(U, 30)
        u = db.get_user(U)
        self.assertEqual((u["currency"], u["chart_days"]), ("rub", 30))

    def test_ensure_user_keeps_balance(self):
        db.buy(U, "TON", 100, 2.0)
        db.ensure_user(U, "renamed", "Имя")
        self.assertAlmostEqual(db.get_user(U)["cash"], 900)

    def test_second_buy_adds_to_holding(self):
        db.buy(U, "TON", 100, 2.0)
        db.buy(U, "TON", 100, 4.0)
        h = db.holding(U, "TON")
        self.assertAlmostEqual(h["amount"], 75)
        self.assertAlmostEqual(h["invested"], 200)

    def test_user_gets_address(self):
        addr = db.get_user(U)["address"]
        self.assertTrue(addr.startswith("UQ") and len(addr) == 48)
        self.assertEqual(db.ensure_user(U, "tester", "Тест")["address"], addr)


class WalletOpsTests(unittest.TestCase):
    """Обмен, перевод, кран, стейкинг — операции, которые раньше жили только в браузере."""

    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        fresh_user()

    def test_swap_usd_to_coin_and_back(self):
        ok, _ = db.swap(U, "USD", "TON", 200, 1.0, 2.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(db.get_user(U)["cash"], 800)
        self.assertAlmostEqual(db.holding(U, "TON")["amount"], 100)
        ok, _ = db.swap(U, "TON", "USD", 50, 3.0, 1.0)   # половину по выросшей цене
        self.assertTrue(ok)
        self.assertAlmostEqual(db.get_user(U)["cash"], 950)
        t = db.trades(U, 1)[0]
        self.assertEqual((t["side"], t["to_symbol"]), ("swap", "USD"))
        self.assertAlmostEqual(t["pnl"], 50)              # 150 получили против 100 себестоимости

    def test_swap_coin_to_coin_moves_cost(self):
        db.buy(U, "ETH", 400, 2000)
        ok, _ = db.swap(U, "ETH", "SOL", 0.1, 2000, 100)
        self.assertTrue(ok)
        self.assertAlmostEqual(db.holding(U, "SOL")["amount"], 2)
        self.assertAlmostEqual(db.holding(U, "SOL")["invested"], 200)
        self.assertAlmostEqual(db.holding(U, "ETH")["invested"], 200)

    def test_swap_rejects_same_asset_and_dust(self):
        self.assertFalse(db.swap(U, "TON", "TON", 1, 2.0, 2.0)[0])
        self.assertFalse(db.swap(U, "USD", "TON", 0.5, 1.0, 2.0)[0])

    def test_send_usd_and_coin(self):
        ok, _ = db.send(U, "USD", 120, "@friend", 1.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(db.get_user(U)["cash"], 880)
        db.buy(U, "TON", 100, 2.0)
        ok, _ = db.send(U, "TON", 10, "UQabcdef", 2.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(db.holding(U, "TON")["amount"], 40)
        self.assertEqual(db.trades(U, 1)[0]["party"], "UQabcdef")

    def test_send_rejects_own_address_and_short(self):
        me = db.get_user(U)["address"]
        self.assertIn("собственный", db.send(U, "USD", 10, me, 1.0)[1])
        self.assertFalse(db.send(U, "USD", 10, "ab", 1.0)[0])

    def test_topup_counts(self):
        db.topup(U)
        db.topup(U)
        u = db.get_user(U)
        self.assertAlmostEqual(u["cash"], 1000 + 2 * db.TOPUP_USD)
        self.assertEqual(u["topups"], 2)
        self.assertEqual(db.trades(U, 1)[0]["side"], "receive")

    def test_stake_and_unstake_with_yield(self):
        db.buy(U, "TON", 200, 2.0)                         # 100 TON
        ok, _ = db.stake(U, 40, 2.0)
        self.assertTrue(ok)
        self.assertAlmostEqual(db.holding(U, "TON")["amount"], 60)
        u = db.get_user(U)
        self.assertAlmostEqual(u["stake_amount"], 40)
        self.assertAlmostEqual(u["stake_invested"], 80)
        # год спустя набежало 4,5%
        earned = db.stake_earned(u, now=u["stake_since"] + 365.25 * 86400)
        self.assertAlmostEqual(earned, 40 * db.STAKE_APY)
        ok, _ = db.unstake(U, 2.0)
        self.assertTrue(ok)
        self.assertGreaterEqual(db.holding(U, "TON")["amount"], 100)
        self.assertEqual(db.get_user(U)["stake_amount"], 0)
        self.assertFalse(db.unstake(U, 2.0)[0])

    def test_stake_more_than_free_fails(self):
        db.buy(U, "TON", 20, 2.0)
        self.assertFalse(db.stake(U, 50, 2.0)[0])

    def test_reset_clears_stake_and_bot_results_but_keeps_settings(self):
        db.buy(U, "TON", 20, 2.0)
        db.stake(U, 5, 2.0)
        db.save_bot(U, {"on": True, "strategy": "grid", "budget": 500, "lots": {"TON": [{}]}, "stats": {"pnl": 9}})
        db.reset_account(U)
        self.assertEqual(db.get_user(U)["stake_amount"], 0)
        bot = trader.load_bot(U)
        self.assertFalse(bot["on"])
        self.assertEqual(bot["lots"], {})
        self.assertEqual(bot["stats"]["pnl"], 0)
        self.assertEqual((bot["strategy"], bot["budget"]), ("grid", 500))

    def test_bot_state_roundtrip_and_active_list(self):
        self.assertEqual(db.get_bot(U), {})
        db.save_bot(U, {"on": True, "coins": ["TON"]})
        self.assertTrue(db.get_bot(U)["on"])
        self.assertIn(U, db.active_bots())
        db.save_bot(U, {"on": False})
        self.assertNotIn(U, db.active_bots())

    def test_trades_sorted_by_time_not_id(self):
        db.buy(U, "TON", 10, 2.0)
        db.buy(U, "TON", 10, 2.0, at=1_000_000, is_bot=True, note="досчёт")   # бот дописал сделку «в прошлое»
        rows = db.trades(U)
        self.assertEqual([bool(r["is_bot"]) for r in rows], [False, True])
        self.assertEqual(rows[1]["note"], "досчёт")


def _acct_with_series(strategy, params=None, coins=("TON",), budget=1000):
    bot = trader.with_defaults({"strategy": strategy, "params": params or {}, "coins": list(coins),
                                "budget": budget, "perTrade": 100})
    return bot, trader.SimAcct(bot)


class StrategyTests(unittest.TestCase):
    """Стратегии — чистые функции над рядом цен; гоняем на синтетике через SimAcct."""

    def run_series(self, bot, acct, prices):
        for i, p in enumerate(prices):
            trader.evaluate(bot, acct, "TON", prices[: i + 1], p, 1000 + i * 3600)

    def test_dip_buys_below_average_and_takes_profit(self):
        bot, acct = _acct_with_series("dip", {"dip": 2.5, "tp": 3, "sl": 4})
        prices = [10.0] * 24 + [9.7] + [10.1]        # просадка 3% → вход; +4.1% → тейк-профит
        self.run_series(bot, acct, prices)
        self.assertEqual([e["side"] for e in acct.log], ["buy", "sell"])
        self.assertGreater(acct.pnl, 0)
        self.assertEqual(acct.trades, 1)
        self.assertEqual(acct.wins, 1)

    def test_dip_stop_loss(self):
        bot, acct = _acct_with_series("dip", {"dip": 2.5, "tp": 3, "sl": 4})
        self.run_series(bot, acct, [10.0] * 24 + [9.7, 9.2])
        self.assertEqual([e["side"] for e in acct.log], ["buy", "sell"])
        self.assertLess(acct.pnl, 0)
        self.assertIn("стоп-лосс", acct.log[-1]["reason"])

    def test_dip_waits_for_enough_history(self):
        bot, acct = _acct_with_series("dip")
        self.run_series(bot, acct, [10.0] * 10 + [5.0])
        self.assertEqual(acct.log, [])

    def test_trend_crossover(self):
        bot, acct = _acct_with_series("trend", {"sl": 5})
        prices = [10.0] * 24 + [11.0] * 6 + [9.0] * 6   # быстрая вверх → вход, затем разворот
        self.run_series(bot, acct, prices)
        self.assertEqual(acct.log[0]["side"], "buy")
        self.assertEqual(acct.log[-1]["side"], "sell")

    def test_grid_ladders_down_and_closes_lots(self):
        bot, acct = _acct_with_series("grid", {"step": 2, "maxLots": 3})
        # три партии вниз (лимит), на 9.2 ничего, на 10.3 все три закрыты и тут же открыта новая первая
        prices = [10.0, 9.75, 9.5, 9.3, 9.2, 10.3]
        self.run_series(bot, acct, prices)
        buys = [e for e in acct.log if e["side"] == "buy"]
        sells = [e for e in acct.log if e["side"] == "sell"]
        self.assertEqual(len(sells), 3)
        self.assertEqual(len(buys), 4)
        self.assertEqual([l["entry"] for l in acct.lots["TON"]], [10.3])
        self.assertEqual(acct.trades, 3)
        self.assertEqual(acct.wins, 3)
        self.assertGreater(acct.pnl, 0)

    def test_budget_caps_sim_account(self):
        bot, acct = _acct_with_series("grid", {"step": 1, "maxLots": 50}, budget=250)
        self.run_series(bot, acct, [10.0 - i * 0.2 for i in range(10)])
        self.assertLessEqual(acct.spent, 250 + 1e-9)
        self.assertEqual(len(acct.lots["TON"]), 2)

    def test_apply_settings_validates(self):
        bot = trader.with_defaults({})
        self.assertIsNone(trader.apply_settings(bot, {"strategy": "grid", "budget": 500, "perTrade": 900,
                                                      "params": {"step": 3, "maxLots": 2.7}, "coins": ["BTC", "XXX", "BTC"]}))
        self.assertEqual(bot["perTrade"], 500)          # не больше бюджета
        self.assertEqual(bot["params"]["maxLots"], 2)   # штуки — целое
        self.assertEqual(bot["coins"], ["BTC"])
        self.assertIsNotNone(trader.apply_settings(bot, {"strategy": "nope"}))
        self.assertIsNotNone(trader.apply_settings(bot, {"coins": ["XXX"]}))
        self.assertIsNotNone(trader.apply_settings(bot, {"budget": "abc"}))

    def test_with_defaults_fills_missing(self):
        bot = trader.with_defaults({"params": {"dip": 9}})
        self.assertEqual(bot["params"]["dip"], 9)
        self.assertEqual(bot["params"]["tp"], 3.0)
        self.assertEqual(bot["stats"]["trades"], 0)


class RealAcctTests(unittest.TestCase):
    """Адаптер над настоящим счётом: партии бота отдельно от holdings, продажа режется по остатку."""

    @classmethod
    def setUpClass(cls):
        db.init_db()

    def setUp(self):
        fresh_user()

    def test_buy_then_sell_records_bot_trades_and_stats(self):
        bot = trader.with_defaults({"budget": 500, "perTrade": 100})
        acct = trader.RealAcct(U, bot)
        acct.buy("TON", 100, 2.0, 5000, "тест")
        self.assertAlmostEqual(db.get_user(U)["cash"], 900)
        self.assertEqual(len(bot["lots"]["TON"]), 1)
        self.assertAlmostEqual(bot["spent"], 100)
        acct.sell("TON", bot["lots"]["TON"][0], 2.5, 6000, "тейк")
        self.assertEqual(bot["lots"], {})
        self.assertAlmostEqual(db.get_user(U)["cash"], 1025)
        self.assertEqual(bot["stats"]["trades"], 1)
        self.assertAlmostEqual(bot["stats"]["pnl"], 25)
        self.assertTrue(all(t["is_bot"] for t in db.trades(U)))
        self.assertEqual(len(acct.events), 2)

    def test_sell_limited_to_what_user_still_holds(self):
        bot = trader.with_defaults({"budget": 500, "perTrade": 100})
        acct = trader.RealAcct(U, bot)
        acct.buy("TON", 100, 2.0, 5000, "тест")        # 50 TON
        db.sell(U, "TON", 30, 2.0)                       # пользователь продал руками
        acct.sell("TON", bot["lots"]["TON"][0], 2.0, 6000, "закрыто")
        self.assertIsNone(db.holding(U, "TON"))
        self.assertEqual(bot["lots"], {})

    def test_budget_respected(self):
        bot = trader.with_defaults({"budget": 150, "perTrade": 100})
        acct = trader.RealAcct(U, bot)
        acct.buy("TON", 100, 2.0, 1, "1")
        acct.buy("TON", 100, 2.0, 2, "2")
        self.assertEqual(len(bot["lots"]["TON"]), 1)


class WebAuthTests(unittest.TestCase):
    def _init_data(self, uid=555, auth_date=None, token=None):
        import hashlib
        import hmac
        import json
        import time
        from urllib.parse import urlencode

        fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAA",
                  "user": json.dumps({"id": uid, "first_name": "Ann", "username": "ann"})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", (token or os.environ["BOT_TOKEN"]).encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        return urlencode(fields)

    def test_valid_init_data(self):
        wu = webauth.parse_init_data(self._init_data())
        self.assertEqual((wu.id, wu.first_name, wu.username), (555, "Ann", "ann"))

    def test_bad_signature_and_stale(self):
        self.assertIsNone(webauth.parse_init_data(self._init_data(token="other")))
        self.assertIsNone(webauth.parse_init_data(self._init_data(auth_date=1)))
        self.assertIsNone(webauth.parse_init_data(""))

    def test_browser_token_roundtrip(self):
        tok = webauth.browser_token(42, now=1000)
        self.assertEqual(webauth.parse_browser_token(tok, now=2000).id, 42)
        self.assertIsNone(webauth.parse_browser_token(tok, now=1000 + webauth.BROWSER_TOKEN_TTL + 1))
        self.assertIsNone(webauth.parse_browser_token(tok[:-1] + "0", now=2000))
        self.assertIsNone(webauth.parse_browser_token("garbage", now=2000))

    def test_authenticate_header(self):
        self.assertEqual(webauth.authenticate("tma " + self._init_data()).id, 555)
        self.assertEqual(webauth.authenticate("tok " + webauth.browser_token(7)).id, 7)
        self.assertIsNone(webauth.authenticate("Bearer x"))
        self.assertIsNone(webauth.authenticate(None))


class ApiTests(unittest.IsolatedAsyncioTestCase):
    """HTTP API целиком: aiohttp-клиент в памяти, цены подменены."""

    UID = 888

    async def asyncSetUp(self):
        import prices
        from aiohttp.test_utils import TestClient, TestServer

        import api

        db.init_db()
        fresh_user(self.UID)
        board = Board({"TON": Quote(2.0, 1.0), "BTC": Quote(70000.0, -1.0), "ETH": Quote(2000.0, 0.5),
                       "SOL": Quote(100.0, 0.0)}, {"usd": 1.0, "rub": 90.0}, 1e12)
        points = [(1_700_000_000 + i * 3600, 2.0) for i in range(48)]

        async def fake_board(force=False):
            return board

        async def fake_history(symbol, days):
            return points

        self._orig = (prices.feed.board, prices.feed.history)
        prices.feed.board, prices.feed.history = fake_board, fake_history
        self.client = TestClient(TestServer(api.make_app()))
        await self.client.start_server()
        self.auth = {"Authorization": "tok " + webauth.browser_token(self.UID)}

    async def asyncTearDown(self):
        import prices
        prices.feed.board, prices.feed.history = self._orig
        await self.client.close()

    async def post(self, path, body=None, expect=200):
        r = await self.client.post(path, json=body or {}, headers=self.auth)
        self.assertEqual(r.status, expect, await r.text())
        return await r.json()

    async def test_requires_auth(self):
        r = await self.client.get("/api/state")
        self.assertEqual(r.status, 401)
        r = await self.client.get("/health")
        self.assertEqual(r.status, 200)

    async def test_state_and_trade_flow(self):
        r = await self.client.get("/api/state", headers=self.auth)
        s = (await r.json())["state"]
        self.assertEqual(s["user"]["cash"], 1000)
        self.assertEqual(s["board"]["quotes"]["TON"]["usd"], 2.0)
        self.assertIn("dip", s["config"]["strats"])

        s = (await self.post("/api/trade", {"side": "buy", "sym": "TON", "value": 100}))["state"]
        self.assertAlmostEqual(s["holdings"]["TON"]["amount"], 50)
        self.assertEqual(s["trades"][0]["side"], "buy")
        self.assertIsInstance(s["trades"][0]["at"], int)

        err = await self.post("/api/trade", {"side": "buy", "sym": "TON", "value": 5000}, expect=400)
        self.assertIn("Не хватает", err["error"])
        err = await self.post("/api/trade", {"side": "buy", "sym": "TON", "value": "abc"}, expect=400)
        self.assertIn("число", err["error"])

        s = (await self.post("/api/swap", {"from": "TON", "to": "ETH", "amount": 10}))["state"]
        self.assertAlmostEqual(s["holdings"]["ETH"]["amount"], 0.01)
        s = (await self.post("/api/send", {"sym": "USD", "amount": 50, "to": "@friend"}))["state"]
        self.assertAlmostEqual(s["user"]["cash"], 850)
        s = (await self.post("/api/topup"))["state"]
        self.assertAlmostEqual(s["user"]["cash"], 1850)
        s = (await self.post("/api/stake", {"amount": 10}))["state"]
        self.assertAlmostEqual(s["stake"]["amount"], 10)
        s = (await self.post("/api/unstake"))["state"]
        self.assertIsNone(s["stake"])
        s = (await self.post("/api/settings", {"currency": "rub", "days": 30}))["state"]
        self.assertEqual((s["user"]["currency"], s["user"]["days"]), ("rub", 30))
        await self.post("/api/settings", {"currency": "gbp"}, expect=400)
        s = (await self.post("/api/reset"))["state"]
        self.assertEqual(s["user"]["cash"], 1000)
        self.assertEqual(s["trades"], [])

    async def test_chart(self):
        r = await self.client.get("/api/chart/TON/7", headers=self.auth)
        self.assertEqual(len((await r.json())["points"]), 48)
        r = await self.client.get("/api/chart/TON/3", headers=self.auth)
        self.assertEqual(r.status, 400)

    async def test_bot_endpoints(self):
        s = (await self.post("/api/bot/settings", {"strategy": "grid", "budget": 400, "perTrade": 100,
                                                    "coins": ["TON"], "params": {"step": 2, "maxLots": 2}}))["state"]
        self.assertEqual(s["bot"]["strategy"], "grid")
        await self.post("/api/bot/settings", {"coins": []}, expect=400)

        r = await self.post("/api/bot/toggle", {"on": True})
        s = r["state"]
        self.assertTrue(s["bot"]["on"])
        # цена ровная: сетка сразу открывает первую партию по текущей цене
        self.assertEqual(len(s["bot"]["lots"]["TON"]), 1)
        self.assertTrue(s["trades"][0]["bot"])
        self.assertIsInstance(s["bot"]["lots"]["TON"][0]["at"], int)

        bt = (await self.post("/api/bot/backtest"))["result"]
        self.assertIn("pnl", bt)
        self.assertIsInstance(bt["from"], int)

        s = (await self.post("/api/bot/close"))["state"]
        self.assertEqual(s["bot"]["lots"], {})
        s = (await self.post("/api/bot/toggle", {"on": False}))["state"]
        self.assertFalse(s["bot"]["on"])

    async def test_dev_seed_disabled_without_dev_user(self):
        await self.post("/api/dev/seed", expect=404)

    async def test_index_served(self):
        r = await self.client.get("/")
        self.assertEqual(r.status, 200)
        self.assertIn("Karman", await r.text())
        self.assertEqual(r.headers["Cache-Control"], "no-store")


class PriceParsingTests(unittest.TestCase):
    def test_parse_simple_price(self):
        board = parse_simple_price({
            "bitcoin": {"usd": 70000, "rub": 6300000, "eur": 63000, "usd_24h_change": -1.5},
            "the-open-network": {"usd": 1.35, "usd_24h_change": 2.0},
            "unknown-coin": {"usd": 1},
        })
        self.assertEqual(set(board.quotes), {"BTC", "TON"})
        self.assertAlmostEqual(board.fx["rub"], 90)
        self.assertAlmostEqual(board.fx["eur"], 0.9)
        self.assertEqual(board.quotes["TON"].change_24h, 2.0)

    def test_empty_payload_raises(self):
        with self.assertRaises(ValueError):
            parse_simple_price({})


class PriceFeedTests(unittest.IsolatedAsyncioTestCase):
    """Кэш графиков: одновременные запросы одного графика идут в сеть один раз,
    протухший кэш отдаётся при ошибке сети."""

    async def test_history_dedups_and_caches(self):
        import asyncio

        from prices import PriceFeed, PricesUnavailable

        feed = PriceFeed()
        calls = []

        async def fake_get(path, **params):
            calls.append(path)
            await asyncio.sleep(0.01)
            return {"prices": [[1_700_000_000_000, 1.0], [1_700_003_600_000, 1.1]]}

        feed._get = fake_get
        a, b = await asyncio.gather(feed.history("TON", 7), feed.history("TON", 7))
        self.assertEqual(a, b)
        self.assertEqual(len(calls), 1)
        await feed.history("TON", 7)
        self.assertEqual(len(calls), 1)            # из кэша

        async def failing_get(path, **params):
            raise PricesUnavailable("429")

        feed._get = failing_get
        feed._charts[("TON", 7)] = (0.0, a)         # протух
        self.assertEqual(await feed.history("TON", 7), a)
        with self.assertRaises(PricesUnavailable):
            await feed.history("BTC", 7)


class FormattingTests(unittest.TestCase):
    def test_money_usd(self):
        self.assertEqual(ui.money(1234.5), "$1 234.50")
        self.assertEqual(ui.money(76934), "$76 934")
        self.assertEqual(ui.money(0.1234), "$0.1234")

    def test_money_rub_uses_rate(self):
        self.assertEqual(ui.money(10, "rub", {"rub": 90.0}), "900 ₽")

    def test_pct_arrows(self):
        self.assertEqual(ui.pct(2.5), "▲ 2.50%")
        self.assertEqual(ui.pct(-0.4), "▼ 0.40%")

    def test_parse_number(self):
        self.assertEqual(parse_number("1 500"), 1500)
        self.assertEqual(parse_number("$100"), 100)
        self.assertEqual(parse_number("1,5"), 1.5)
        self.assertIsNone(parse_number("abc"))
        self.assertIsNone(parse_number("nan"))

    def test_welcome_escapes_html(self):
        self.assertIn("&lt;b&gt;", ui.welcome_text("<b>x</b>"))

    def test_wallet_text_with_holdings(self):
        db.init_db()
        fresh_user()
        db.buy(U, "TON", 100, 2.0)
        board = Board({"TON": Quote(4.0, 10.0)}, {"usd": 1.0}, fetched_at=1e12)
        text = ui.wallet_text(db.get_user(U), db.holdings(U), board)
        self.assertIn("$1 100", text)      # 900 наличных + 50 TON по $4
        self.assertIn("+$100", text)            # прибыль по позиции

    def test_wallet_text_shows_stake_and_bot(self):
        db.init_db()
        fresh_user()
        db.buy(U, "TON", 100, 2.0)
        db.stake(U, 20, 2.0)
        board = Board({"TON": Quote(2.0, 0.0)}, {"usd": 1.0}, fetched_at=1e12)
        text = ui.wallet_text(db.get_user(U), db.holdings(U), board, {"on": True, "stats": {"trades": 3, "pnl": 1.5}})
        self.assertIn("В стейкинге", text)
        self.assertIn("$1" + ui.NBSP + "000", text)   # стейк не выпадает из общего баланса
        self.assertIn("Автотрейдер работает", text)

    def test_history_lines_cover_all_sides(self):
        db.init_db()
        fresh_user()
        db.buy(U, "TON", 100, 2.0)
        db.swap(U, "TON", "USD", 10, 2.0, 1.0)
        db.send(U, "USD", 10, "<b>x</b>", 1.0)
        db.topup(U)
        db.stake(U, 5, 2.0)
        db.unstake(U, 2.0)
        text = ui.history_text(db.trades(U), "usd", {"usd": 1.0})
        for word in ("Купил", "Обмен", "Перевод", "Пополнение", "В стейкинг", "Из стейкинга"):
            self.assertIn(word, text)
        self.assertIn("&lt;b&gt;", text)

    def test_bot_events_text(self):
        text = ui.bot_events_text([{"side": "buy", "s": "TON", "usd": 100, "price": 2.0, "reason": "a<b"},
                                   {"side": "sell", "s": "TON", "usd": 110, "price": 2.2, "reason": "tp", "pnl": 10}],
                                  "usd", {"usd": 1.0})
        self.assertIn("Купил TON", text)
        self.assertIn("+$10", text)
        self.assertIn("a&lt;b", text)

    def test_app_keyboard_browser_link_carries_token(self):
        kb = ui.app_keyboard(42)
        url = kb.inline_keyboard[1][0].url
        self.assertTrue(url.startswith("https://example.test/karman/?t=42."))
        self.assertEqual(webauth.parse_browser_token(url.split("?t=")[1]).id, 42)


class ChartTests(unittest.TestCase):
    def test_render_returns_png(self):
        points = [(1_700_000_000 + i * 3600, 1.0 + (i % 5) * 0.01) for i in range(48)]
        png = render_chart("TON", points, 1)
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 10_000)


class _Recorder:
    """Собирает всё, что бот «отправил» — answer/edit на сообщении и answer на callback."""

    def __init__(self):
        self.sent: list[tuple[str, str]] = []   # (метод, текст)
        self.alerts: list[str] = []


class _FakeUser:
    def __init__(self, uid):
        self.id, self.username, self.first_name = uid, "tester", "Тест"


class _FakeBot:
    def __init__(self, rec: _Recorder):
        self.rec = rec

    async def set_chat_menu_button(self, chat_id=None, menu_button=None):
        self.rec.sent.append(("menu_button", menu_button.web_app.url))


class _FakeMessage:
    def __init__(self, rec: _Recorder, uid: int, text: str | None = "x", photo=None):
        self.rec, self.from_user, self.text, self.photo = rec, _FakeUser(uid), text, photo
        self.message_id = 1
        self.bot = _FakeBot(rec)

    async def answer(self, text, reply_markup=None, **_):
        self.rec.sent.append(("answer", text))
        return _FakeMessage(self.rec, self.from_user.id, text)

    async def answer_photo(self, photo, caption=None, reply_markup=None, **_):
        self.rec.sent.append(("photo", caption or ""))

    async def edit_text(self, text, reply_markup=None, **_):
        self.rec.sent.append(("edit", text))

    async def edit_media(self, media, reply_markup=None, **_):
        self.rec.sent.append(("edit_media", media.caption or ""))


class _FakeCallback:
    def __init__(self, rec: _Recorder, uid: int, data: str, message: _FakeMessage):
        self.rec, self.from_user, self.data, self.message = rec, _FakeUser(uid), data, message

    async def answer(self, text=None, show_alert=False, **_):
        if show_alert:
            self.rec.alerts.append(text or "")


class FlowTests(unittest.IsolatedAsyncioTestCase):
    """Сквозной сценарий через настоящие хендлеры, без сети: цены подменены."""

    UID = 777

    async def asyncSetUp(self):
        import prices
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.base import StorageKey
        from aiogram.fsm.storage.memory import MemoryStorage

        db.init_db()
        fresh_user(self.UID)
        self.rec = _Recorder()
        self.state = FSMContext(MemoryStorage(), StorageKey(bot_id=1, chat_id=self.UID, user_id=self.UID))

        board = Board({"TON": Quote(2.0, 1.0), "BTC": Quote(70000.0, -1.0)}, {"usd": 1.0, "rub": 90.0}, 1e12)
        points = [(1_700_000_000 + i * 3600, 2.0) for i in range(24)]

        async def fake_board(force=False):
            return board

        async def fake_history(symbol, days):
            return points

        self._orig = (prices.feed.board, prices.feed.history)
        prices.feed.board, prices.feed.history = fake_board, fake_history

    async def asyncTearDown(self):
        import prices
        prices.feed.board, prices.feed.history = self._orig

    def msg(self, text="x"):
        return _FakeMessage(self.rec, self.UID, text)

    def cb(self, data, message=None):
        return _FakeCallback(self.rec, self.UID, data, message or self.msg())

    async def test_full_trade_cycle(self):
        from handlers import common, market, trade, wallet

        await common.start(self.msg("/start"), self.state)
        self.assertIn("Karman", self.rec.sent[0][1])
        self.assertIn(("menu_button", "https://example.test/karman/"), self.rec.sent)
        self.assertIn("Общий баланс", self.rec.sent[-1][1])

        await market.market_msg(self.msg(ui.BTN_MARKET), self.state)
        self.assertIn("Рынок", self.rec.sent[-1][1])

        await market.open_coin(self.cb("coin:TON"), self.state)
        self.assertEqual(self.rec.sent[-1][0], "photo")
        self.assertIn("Toncoin", self.rec.sent[-1][1])

        await trade.buy_start(self.cb("buy:TON"), self.state)
        self.assertIn("Покупка TON", self.rec.sent[-1][1])
        self.assertEqual(await self.state.get_state(), trade.Trade.amount.state)

        await trade.buy_pick(self.cb("buyq:TON:100"), self.state)
        self.assertIn("Купить", self.rec.sent[-1][1])
        self.assertIsNone(await self.state.get_state())

        await trade.confirm(self.cb("conf:buy:TON:100"), self.state)
        self.assertIn("✅", self.rec.sent[-1][1])
        self.assertAlmostEqual(db.holding(self.UID, "TON")["amount"], 50)

        await trade.sell_start(self.cb("sell:TON"), self.state)
        await trade.sell_pick(self.cb("sellp:TON:100"), self.state)
        await trade.confirm(self.cb("conf:sell:TON:-1"), self.state)
        self.assertIsNone(db.holding(self.UID, "TON"))
        self.assertAlmostEqual(db.get_user(self.UID)["cash"], 1000)

        await wallet.wallet_cb(self.cb("wallet"), self.state)
        self.assertIn("Портфель пустой", self.rec.sent[-1][1])

    async def test_typed_amount_and_rejections(self):
        from handlers import trade

        await trade.buy_start(self.cb("buy:BTC"), self.state)
        await trade.amount_typed(self.msg("не число"), self.state)
        self.assertIn("Нужно число", self.rec.sent[-1][1])
        await trade.amount_typed(self.msg("5 000"), self.state)
        self.assertIn("Не хватает", self.rec.sent[-1][1])
        self.assertEqual(await self.state.get_state(), trade.Trade.amount.state)
        await trade.amount_typed(self.msg("$700"), self.state)
        self.assertIn("0.010000 BTC", self.rec.sent[-1][1])

        await trade.sell_pick(self.cb("sellp:BTC:50"), self.state)
        self.assertIn("нет BTC", self.rec.alerts[-1])

    async def test_settings_and_reset(self):
        from handlers import wallet

        await wallet.set_currency(self.cb("set:cur:rub"))
        self.assertIn("Рубли", self.rec.sent[-1][1])
        await wallet.set_days(self.cb("set:days:30"))
        self.assertEqual(db.get_user(self.UID)["chart_days"], 30)
        db.buy(self.UID, "TON", 100, 2.0)
        await wallet.reset_do(self.cb("reset:yes"), self.state)
        self.assertEqual(db.holdings(self.UID), [])
        self.assertIn("₽", self.rec.sent[-1][1])


class WiringTests(unittest.TestCase):
    def test_dispatcher_builds(self):
        from bot import build_dispatcher
        dp = build_dispatcher()
        names = [r.name for r in dp.sub_routers]
        self.assertEqual(names[0], "common")
        self.assertEqual(names[-1], "fallback")
        self.assertLess(names.index("wallet"), names.index("trade"))


if __name__ == "__main__":
    unittest.main()
