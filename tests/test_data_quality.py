import unittest
from launchlab.models import Snapshot, SecurityObservation
from launchlab.storage import connect, add_snapshots, add_securities
from scripts.data_quality import assess


class QualityTests(unittest.TestCase):
    def test_empty_database_counts_zero(self):
        with connect(':memory:') as db:
            result = assess(db)
        self.assertEqual(result['observations'], 0)
        self.assertEqual(result['security_complete_observations'], 0)
        self.assertIsNone(result['first_observed_at'])

    def test_coverage_gaps_and_security_completeness(self):
        with connect(':memory:') as db:
            observations = [Snapshot(pool='pool', token='mint', observed_at=t,
                                     price_usd=1, liquidity_usd=50000)
                            for t in (1799990000, 1799990060, 1799991100)]
            add_snapshots(db, observations)
            add_securities(db, [SecurityObservation('mint', 1799990000, True, True, None, 'test'),
                                SecurityObservation('mint', 1799990100, True, True, 15, 'test')])
            result = assess(db)
        self.assertEqual(result['observations'], 3)
        self.assertEqual(result['pools_observed'], 1)
        self.assertEqual(result['pools_with_at_least_three_observations'], 1)
        self.assertEqual(result['gaps_over_15_minutes_between_quotes'], 1)
        self.assertEqual(result['security_complete_observations'], 1)
        self.assertEqual(result['security_top10_raw_accounts_known'], 1)


if __name__ == '__main__':
    unittest.main()
