import unittest
from unittest.mock import patch

from market_sim.engine import MatchingEngine
from market_sim.simulation import HighFrequencyTrader, InstitutionalTrader, RandomTrader


def agent_test_config():
    return {
        "seed_order_book": False,
        "latent_liquidity": False,
        "enforce_risk_limits": False,
        "maker_fee_rate": 0.0,
        "taker_fee_rate": 0.0,
        "liquidity_probability": 0.0,
        "news_probability": 0.0,
        "max_resting_order_deviation": 0.0,
    }


class AgentActionTests(unittest.TestCase):
    def make_engine(self):
        return MatchingEngine(start_price=100.0, config=agent_test_config())

    def test_institutional_trader_can_buy_sell_and_hold(self):
        for side in ("buy", "sell"):
            engine = self.make_engine()
            agent = InstitutionalTrader(
                owner=f"institution-{side}",
                agent_type="institutional",
                cash=1_000_000,
                parent_side=side,
                parent_remaining=1000,
                urgency=1.0,
            )
            agent.attach(engine)
            with patch("market_sim.simulation.random.random", return_value=0.0):
                agent.act(engine)
            self.assertEqual(engine.accounts[agent.owner].extra["last_action"], side)

        engine = self.make_engine()
        agent = InstitutionalTrader(
            owner="institution-hold",
            agent_type="institutional",
            cash=1_000_000,
            parent_side="buy",
            parent_remaining=1000,
            urgency=1.0,
        )
        agent.attach(engine)
        with patch("market_sim.simulation.random.random", return_value=0.99):
            agent.act(engine)
        self.assertEqual(engine.accounts[agent.owner].extra["last_action"], "hold")

    def test_high_frequency_trader_can_quote_both_sides_and_hold(self):
        engine = self.make_engine()
        agent = HighFrequencyTrader(owner="hft-active", agent_type="high-frequency", cash=750_000)
        agent.attach(engine)
        with patch("market_sim.simulation.random.random", return_value=0.0):
            agent.act(engine)
        self.assertEqual(engine.accounts[agent.owner].extra["last_action"], "buy/sell")

        engine = self.make_engine()
        agent = HighFrequencyTrader(owner="hft-hold", agent_type="high-frequency", cash=750_000)
        agent.attach(engine)
        with patch("market_sim.simulation.random.random", return_value=0.99):
            agent.act(engine)
        self.assertEqual(engine.accounts[agent.owner].extra["last_action"], "hold")

    def test_random_trader_can_buy_sell_and_hold(self):
        for side in ("buy", "sell"):
            engine = self.make_engine()
            agent = RandomTrader(owner=f"random-{side}", agent_type="random", cash=75_000, activity_rate=1.0)
            agent.attach(engine)
            with patch("market_sim.simulation.random.random", side_effect=[0.0, 0.0]), patch(
                "market_sim.simulation.random.choice", return_value=side
            ):
                agent.act(engine)
            self.assertEqual(engine.accounts[agent.owner].extra["last_action"], side)

        engine = self.make_engine()
        agent = RandomTrader(owner="random-hold", agent_type="random", cash=75_000, activity_rate=0.0)
        agent.attach(engine)
        with patch("market_sim.simulation.random.random", return_value=1.0):
            agent.act(engine)
        self.assertEqual(engine.accounts[agent.owner].extra["last_action"], "hold")


if __name__ == "__main__":
    unittest.main()
