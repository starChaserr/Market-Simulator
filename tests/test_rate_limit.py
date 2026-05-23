import unittest

from main import ApiRateLimiter


class ApiRateLimiterTests(unittest.TestCase):
    def test_per_second_limit_rejects_until_window_moves(self):
        limiter = ApiRateLimiter(enabled=True, per_second=2, per_minute=100)

        self.assertTrue(limiter.check("user:alpha", now=100.0)["allowed"])
        self.assertTrue(limiter.check("user:alpha", now=100.1)["allowed"])
        rejected = limiter.check("user:alpha", now=100.2)
        self.assertFalse(rejected["allowed"])
        self.assertAlmostEqual(rejected["retry_after"], 0.8)
        self.assertEqual(rejected["headers"]["X-RateLimit-Remaining-Second"], "0")

        self.assertTrue(limiter.check("user:alpha", now=101.0)["allowed"])

    def test_per_minute_limit_rejects_until_window_moves(self):
        limiter = ApiRateLimiter(enabled=True, per_second=100, per_minute=2)

        self.assertTrue(limiter.check("user:alpha", now=100.0)["allowed"])
        self.assertTrue(limiter.check("user:alpha", now=101.0)["allowed"])
        rejected = limiter.check("user:alpha", now=102.0)
        self.assertFalse(rejected["allowed"])
        self.assertAlmostEqual(rejected["retry_after"], 58.0)
        self.assertEqual(rejected["headers"]["X-RateLimit-Remaining-Minute"], "0")

        self.assertTrue(limiter.check("user:alpha", now=160.0)["allowed"])

    def test_keys_are_limited_independently(self):
        limiter = ApiRateLimiter(enabled=True, per_second=1, per_minute=10)

        self.assertTrue(limiter.check("user:alpha", now=100.0)["allowed"])
        self.assertTrue(limiter.check("user:beta", now=100.0)["allowed"])
        self.assertFalse(limiter.check("user:alpha", now=100.2)["allowed"])

    def test_disabled_limiter_allows_requests(self):
        limiter = ApiRateLimiter(enabled=False, per_second=1, per_minute=1)

        self.assertTrue(limiter.check("user:alpha", now=100.0)["allowed"])
        self.assertTrue(limiter.check("user:alpha", now=100.0)["allowed"])


if __name__ == "__main__":
    unittest.main()
