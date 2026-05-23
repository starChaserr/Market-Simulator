import unittest

from market_sim import MarketSimulator


class ChartRefreshTests(unittest.TestCase):
    def test_chart_refresh_settings_are_exposed_and_clamped(self):
        simulator = MarketSimulator(tick_interval=0.05)
        try:
            settings = simulator.set_chart_refresh_interval(0.01)
            self.assertEqual(settings["chart_refresh_interval"], simulator.MIN_CHART_REFRESH_INTERVAL)
            self.assertEqual(settings["chart_refresh_ms"], 250)

            settings = simulator.set_chart_refresh_interval(120)
            self.assertEqual(settings["chart_refresh_interval"], simulator.MAX_CHART_REFRESH_INTERVAL)
            self.assertEqual(settings["chart_refresh_ms"], 60000)

            settings = simulator.set_chart_refresh_interval(5)
            self.assertEqual(settings["chart_refresh_interval"], 5)
            self.assertEqual(settings["chart_refresh_ms"], 5000)
            self.assertEqual(simulator.snapshot()["chart_refresh_ms"], 5000)
        finally:
            simulator.stop()


if __name__ == "__main__":
    unittest.main()
