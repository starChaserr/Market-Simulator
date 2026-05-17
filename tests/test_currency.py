import unittest

from market_sim.currency import currency_preferences, region_from_accept_language, region_from_locale, region_from_timezone


class CurrencyTests(unittest.TestCase):
    def test_region_from_locale(self):
        self.assertEqual(region_from_locale("en-US"), "US")
        self.assertEqual(region_from_locale("en_IN"), "IN")
        self.assertEqual(region_from_locale("zh-Hant-TW"), "TW")

    def test_region_from_accept_language(self):
        self.assertEqual(region_from_accept_language("en-IN,en;q=0.9"), "IN")

    def test_region_from_timezone(self):
        self.assertEqual(region_from_timezone("Asia/Kolkata"), "IN")
        self.assertEqual(region_from_timezone("Asia/Calcutta"), "IN")
        self.assertEqual(region_from_timezone("Europe/London"), "GB")

    def test_currency_precedence(self):
        self.assertEqual(currency_preferences(explicit_currency="eur", locale="en-US")["currency"], "EUR")
        self.assertEqual(currency_preferences(locale="en-IN")["currency"], "INR")
        self.assertEqual(currency_preferences(timezone="Asia/Tokyo")["currency"], "JPY")
        self.assertEqual(currency_preferences(locale="en-US", timezone="Asia/Kolkata")["currency"], "INR")
        self.assertEqual(currency_preferences()["currency"], "USD")


if __name__ == "__main__":
    unittest.main()
