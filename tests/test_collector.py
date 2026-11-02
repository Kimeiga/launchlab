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


class CollectorFallbackTests(unittest.TestCase):
    @patch('launchlab.cli.fetch_onchain_security')
    @patch('launchlab.cli.fetch_token_security', side_effect=RuntimeError('404'))
    @patch('launchlab.cli.fetch_pools_by_address', return_value=[])
    @patch('launchlab.cli.fetch_new_pools')
    @patch('launchlab.cli.time.time', return_value=1800000000)
    def test_falls_back_to_onchain_when_metadata_is_missing(self, clock, new_pools, multi, info, rpc):
        s = Snapshot(pool='3'*32, token='4'*32, observed_at=1799999990,
                     price_usd=.1, liquidity_usd=60_000, source='geckoterminal')
        new_pools.return_value = [s]
        rpc.return_value = SecurityObservation(s.token, 1800000000, True, True, 12,
                                               source='solana_rpc_raw_token_accounts')
        db = connect(':memory:')
        self.assertEqual(collect_once(db, max_tracked=0, max_enrich=1), (1, 1, 0, 1))
        self.assertEqual(securities_all(db)[0].source, 'solana_rpc_raw_token_accounts')
        rpc.assert_called_once_with(s.token)
        db.close()

    @patch('launchlab.cli.fetch_onchain_security', side_effect=RuntimeError('RPC unavailable'))
    @patch('launchlab.cli.fetch_token_security', side_effect=RuntimeError('Gecko unavailable'))
    @patch('launchlab.cli.fetch_pools_by_address', return_value=[])
    @patch('launchlab.cli.fetch_new_pools')
    @patch('launchlab.cli.time.time', return_value=1800000000)
    def test_missing_both_sources_never_fabricates_safety(self, clock, new_pools, multi, info, rpc):
        s = Snapshot(pool='3'*32, token='4'*32, observed_at=1799999990,
                     price_usd=.1, liquidity_usd=60_000, source='geckoterminal')
        new_pools.return_value = [s]
        db = connect(':memory:')
        self.assertEqual(collect_once(db, max_tracked=0, max_enrich=1), (1, 1, 0, 0))
        self.assertEqual(securities_all(db), [])
        db.close()

    @patch('launchlab.cli.fetch_pools_by_address', return_value=[])
    @patch('launchlab.cli.fetch_new_pools')
    @patch('launchlab.cli.time.time', return_value=1800000000)
    def test_multiple_pages_deduplicate_pool_ids(self, clock, fetch, refresh):
        s = Snapshot(pool='3'*32, token='4'*32, observed_at=1799999990,
                     price_usd=.1, liquidity_usd=100, source='geckoterminal')
        fetch.side_effect = [[s], [s]]
        db = connect(':memory:')
        self.assertEqual(collect_once(db, max_tracked=0, max_enrich=0, new_pages=2), (1, 1, 0, 0))
        self.assertEqual(fetch.call_count, 2)
        db.close()
