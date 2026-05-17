import unittest
import time

from market_sim.engine import MatchingEngine, Order


def test_config():
    return {
        "seed_order_book": False,
        "latent_liquidity": False,
        "enforce_risk_limits": True,
        "api_starting_cash": 1_000_000.0,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "max_order_quantity": 1_000_000.0,
        "max_position_abs": 1_000_000.0,
        "allow_short": True,
        "liquidity_probability": 0.0,
        "news_probability": 0.0,
        "max_resting_order_deviation": 0.0,
    }


class MatchingEngineTests(unittest.TestCase):
    def make_engine(self):
        return MatchingEngine(start_price=100.0, config=test_config())

    def test_price_time_priority_and_partial_fill(self):
        engine = self.make_engine()
        first = engine.submit_order(
            side="sell",
            quantity=50,
            price=101,
            order_type="limit",
            owner="maker-1",
            agent_type="background-liquidity",
        )
        second = engine.submit_order(
            side="sell",
            quantity=50,
            price=101,
            order_type="limit",
            owner="maker-2",
            agent_type="background-liquidity",
        )

        result = engine.submit_order(
            side="buy",
            quantity=70,
            price=101,
            order_type="limit",
            owner="api-a",
            agent_type="api-user",
        )

        self.assertEqual(result["status"], "filled")
        self.assertEqual(result["filled_quantity"], 70)
        self.assertEqual(engine.orders[first["order_id"]].status, "filled")
        self.assertEqual(engine.orders[second["order_id"]].status, "partially_filled")
        self.assertAlmostEqual(engine.orders[second["order_id"]].remaining, 30)

    def test_post_only_rejects_crossing_limit(self):
        engine = self.make_engine()
        engine.submit_order(side="sell", quantity=10, price=100, order_type="limit", owner="maker", agent_type="background-liquidity")
        result = engine.submit_order(
            side="buy",
            quantity=10,
            price=100,
            order_type="limit",
            owner="api-a",
            agent_type="api-user",
            post_only=True,
        )
        self.assertEqual(result["status"], "rejected")
        self.assertIn("post_only", result["reject_reason"])

    def test_ioc_cancels_unfilled_remainder(self):
        engine = self.make_engine()
        engine.submit_order(side="sell", quantity=10, price=100, order_type="limit", owner="maker", agent_type="background-liquidity")
        result = engine.submit_order(
            side="buy",
            quantity=25,
            price=100,
            order_type="limit",
            owner="api-a",
            agent_type="api-user",
            time_in_force="ioc",
        )
        self.assertEqual(result["status"], "partial_canceled")
        self.assertEqual(result["filled_quantity"], 10)
        self.assertEqual(len(engine.bids), 0)

    def test_fok_rejects_when_not_enough_visible_liquidity(self):
        engine = self.make_engine()
        engine.submit_order(side="sell", quantity=10, price=100, order_type="limit", owner="maker", agent_type="background-liquidity")
        result = engine.submit_order(
            side="buy",
            quantity=25,
            price=100,
            order_type="limit",
            owner="api-a",
            agent_type="api-user",
            time_in_force="fok",
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(engine.orders[result["order_id"]].filled_quantity, 0)

    def test_cancel_open_order(self):
        engine = self.make_engine()
        result = engine.submit_order(side="buy", quantity=25, price=99, order_type="limit", owner="api-a", agent_type="api-user")
        canceled = engine.cancel_order(result["order_id"], owner="api-a")
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(len(engine.bids), 0)

    def test_stop_order_triggers(self):
        engine = self.make_engine()
        engine.submit_order(side="sell", quantity=20, price=101, order_type="limit", owner="maker", agent_type="background-liquidity")
        stop = engine.submit_order(
            side="buy",
            quantity=10,
            order_type="stop",
            stop_price=100.5,
            owner="api-a",
            agent_type="api-user",
        )
        self.assertEqual(stop["status"], "pending_trigger")
        engine.submit_order(side="buy", quantity=5, price=101, order_type="limit", owner="api-b", agent_type="api-user")
        triggered = engine.orders[stop["order_id"]]
        self.assertIn(triggered.status, {"filled", "partial_canceled", "canceled"})
        self.assertIsNotNone(triggered.triggered_at)

    def test_account_pnl_tracks_realized_and_fees(self):
        engine = self.make_engine()
        engine.submit_order(side="sell", quantity=100, price=100, order_type="limit", owner="maker", agent_type="background-liquidity")
        engine.submit_order(side="buy", quantity=100, price=100, order_type="limit", owner="api-a", agent_type="api-user")
        engine.submit_order(side="buy", quantity=100, price=110, order_type="limit", owner="maker-2", agent_type="background-liquidity")
        engine.submit_order(side="sell", quantity=100, price=110, order_type="limit", owner="api-a", agent_type="api-user")
        account = engine.account_summary("api-a")
        self.assertAlmostEqual(account["realized_pnl"], 1000)
        self.assertAlmostEqual(account["inventory"], 0)

    def test_funding_account_adds_capital_without_pnl(self):
        engine = self.make_engine()
        account = engine.add_account_funds("api-funded", 25_000, agent_type="api-user")

        self.assertAlmostEqual(account["cash"], 25_000)
        self.assertAlmostEqual(account["initial_cash"], 25_000)
        self.assertAlmostEqual(account["profit_loss"], 0)

    def test_reference_mid_price_clamps_stale_book(self):
        engine = self.make_engine()
        engine.config["max_reference_deviation"] = 0.05
        now = time.time()
        engine.bids = [
            Order(
                id="bid-stale",
                side="buy",
                quantity=10,
                remaining=10,
                price=150,
                order_type="limit",
                owner="maker",
                agent_type="high-frequency",
                created_at=now,
            )
        ]
        engine.asks = [
            Order(
                id="ask-stale",
                side="sell",
                quantity=10,
                remaining=10,
                price=160,
                order_type="limit",
                owner="maker",
                agent_type="high-frequency",
                created_at=now,
            )
        ]

        self.assertEqual(engine.mid_price, 155)
        self.assertAlmostEqual(engine.reference_mid_price, 105)

    def test_internal_outlier_orders_expire(self):
        engine = self.make_engine()
        engine.config["max_resting_order_deviation"] = 0.05
        result = engine.submit_order(
            side="buy",
            quantity=10,
            price=130,
            order_type="limit",
            owner="market-maker-test",
            agent_type="high-frequency",
        )

        self.assertEqual(result["status"], "open")
        self.assertEqual(len(engine.bids), 1)
        engine.advance_environment()
        self.assertEqual(len(engine.bids), 0)
        self.assertEqual(engine.orders[result["order_id"]].status, "expired")
        self.assertIn("reference band", engine.orders[result["order_id"]].reject_reason)

    def test_no_trade_history_uses_mark_price(self):
        engine = self.make_engine()
        engine.config["max_reference_deviation"] = 0.2
        engine.submit_order(side="buy", quantity=10, price=104, order_type="limit", owner="maker-a", agent_type="background-liquidity")
        engine.submit_order(side="sell", quantity=10, price=106, order_type="limit", owner="maker-b", agent_type="background-liquidity")

        engine.record_history()
        latest = engine.history[-1]
        self.assertAlmostEqual(latest["close"], 105)
        self.assertAlmostEqual(latest["last_trade"], 100)


if __name__ == "__main__":
    unittest.main()
