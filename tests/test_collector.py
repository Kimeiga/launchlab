import unittest
from unittest.mock import patch
from launchlab.cli import collect_once
from launchlab.models import Snapshot, SecurityObservation
from launchlab.storage import connect, securities_all, snapshots_all


class CollectorTests(unittest.TestCase):
    @patch('launchlab.cli.fetch_token_security')
    @patch('launchlab.cli.fetch_pools_by_address')
    @patch('launchlab.cli.fetch_new_pools')
    @patch('launchlab.cli.time.time', return_value=1800000000)
    def test_collect_and_enrich_end_to_end_without_network(self, clock, fetch_new, fetch_multi, fetch_security):
        a = Snapshot(pool='33333333333333333333333333333333', token='44444444444444444444444444444444',
                    observed_at=1799999990, price_usd=.0001, liquidity_usd=60000,
                    source='geckoterminal')
        b = Snapshot(pool=a.pool, token=a.token, observed_at=1800000001,
                    price_usd=.00011, liquidity_usd=60000, source='geckoterminal')
        fetch_new.return_value = [a]
        fetch_multi.return_value = [b]
        fetch_security.return_value = SecurityObservation(a.token, 1800000001, True, True, 12,
                                                          source='geckoterminal_token_info')
        db = connect(':memory:')
        self.assertEqual(collect_once(db, max_tracked=10, max_enrich=1), (1, 1, 1, 1))
        self.assertEqual(len(snapshots_all(db)), 2)
        self.assertEqual(len(securities_all(db)), 1)
        self.assertEqual(fetch_multi.call_count, 1)
        self.assertEqual(fetch_security.call_count, 1)
        db.close()

    @patch('launchlab.cli.fetch_new_pools', return_value=[])
    def test_empty_collector(self, fetch):
        db = connect(':memory:')
        self.assertEqual(collect_once(db, max_tracked=0, max_enrich=0), (0, 0, 0, 0))
        db.close()
