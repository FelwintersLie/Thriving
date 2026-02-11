import unittest

from app.health_check import run_health_check


class HealthCheckTests(unittest.TestCase):
    def test_health_check_runs_and_passes(self):
        result = run_health_check()
        self.assertIn("all_ok", result)
        self.assertTrue(result["all_ok"])
        self.assertGreater(result["assignment_count"], 0)


if __name__ == "__main__":
    unittest.main()
