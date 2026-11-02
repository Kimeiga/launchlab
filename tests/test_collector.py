import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from launchlab.cli import collect_once
from launchlab.models import Snapshot, SecurityObservation
from launchlab.storage import add_snapshots, connect, securities_all, snapshots_all
from scripts.data_quality import assess


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
        fetch_new.side_effect = [[a], []]
        fetch_multi.return_value = [b]
        fetch_security.return_value = SecurityObservation(a.token, 1800000001, True, True, 12,
                                                          source='geckoterminal_token_info')
        db = connect(':memory:')
        # A first-cycle discovery is already sampled; refresh next cycle.
        self.assertEqual(collect_once(db, max_tracked=10, max_enrich=1), (1, 1, 0, 1))
        self.assertEqual(collect_once(db, max_tracked=10, max_enrich=1), (0, 0, 1, 0))
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


BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def discovery_page(cycle: int, page: int, observed_at: int) -> list[Snapshot]:
    """Twenty unique, valid-looking addresses per page and per cycle."""
    return [
        Snapshot(pool=f"{cycle}{page}" + "1" * 28 + BASE58[i] + BASE58[i + 20],
                 token="4" * 32, observed_at=observed_at - 10,
                 price_usd=1 + i / 100, liquidity_usd=1000, source="geckoterminal")
        for i in range(20)
    ]


class RefreshSchedulingTests(unittest.TestCase):
    @patch("launchlab.cli.fetch_pools_by_address")
    @patch("launchlab.cli.fetch_new_pools")
    @patch("launchlab.cli.time.time", return_value=1800000000)
    def test_sixty_discoveries_cannot_starve_prior_pool(self, clock, discovery, multi):
        old = Snapshot(pool="9" * 32, token="4" * 32, observed_at=1799999000,
                       price_usd=1, liquidity_usd=1000, source="geckoterminal")
        discovery.side_effect = [
            discovery_page(1, page, 1800000000) for page in range(1, 4)
        ]
        multi.return_value = [
            Snapshot(pool=old.pool, token=old.token, observed_at=1800000000,
                     price_usd=1.5, liquidity_usd=1000, source="geckoterminal")
        ]
        with connect(":memory:") as db:
            add_snapshots(db, [old])
            self.assertEqual(collect_once(db, max_tracked=40, max_enrich=0,
                                          new_pages=3), (60, 60, 1, 0))
            observations = [s for s in snapshots_all(db) if s.pool == old.pool]
            self.assertEqual([s.observed_at for s in observations],
                             [1799999000, 1800000000])
            self.assertEqual(assess(db)["pools_observed"], 61)
        self.assertEqual(discovery.call_count, 3)
        self.assertEqual(multi.call_count, 1)
        self.assertEqual(multi.call_args.args[0], [old.pool])

    @patch("launchlab.cli.fetch_pools_by_address")
    @patch("launchlab.cli.fetch_new_pools")
    @patch("launchlab.cli.time.time")
    def test_repeated_cycles_build_actual_three_point_series(self, clock, discovery, multi):
        now = [1800000000]
        cycle = [1]
        clock.side_effect = lambda: now[0]
        discovery.side_effect = lambda page=1: discovery_page(cycle[0], page, now[0])
        multi.side_effect = lambda addresses: [
            Snapshot(pool=pool, token="4" * 32, observed_at=now[0],
                     price_usd=1 + cycle[0] / 2, liquidity_usd=1000,
                     source="geckoterminal") for pool in addresses
        ]
        tracked = "9" * 32
        with connect(":memory:") as db:
            add_snapshots(db, [
                Snapshot(pool=tracked, token="4" * 32, observed_at=now[0] - 7200,
                         price_usd=1, liquidity_usd=1000, source="geckoterminal")
            ])
            for collection in (1, 2):
                cycle[0] = collection
                now[0] = 1800000000 + (collection - 1) * 3600
                previous_calls = multi.call_count
                result = collect_once(db, max_tracked=40, max_enrich=0, new_pages=3)
                self.assertEqual(result[:2], (60, 60))
                self.assertLessEqual(multi.call_count - previous_calls, 2)
            samples = [s for s in snapshots_all(db) if s.pool == tracked]
            self.assertEqual(len(samples), 3)
            self.assertEqual(len({s.observed_at for s in samples}), 3)
            self.assertEqual(len({s.price_usd for s in samples}), 3)
            quality = assess(db)
            self.assertGreaterEqual(quality["pools_with_at_least_three_observations"], 1)
            self.assertEqual(quality["pools_observed"], 121)
        self.assertEqual(discovery.call_count, 6)

    @patch("launchlab.cli.fetch_pools_by_address", return_value=[])
    @patch("launchlab.cli.fetch_new_pools")
    @patch("launchlab.cli.time.time", return_value=1800000000)
    def test_refresh_budget_preserves_series_and_admits_new_cohorts(
            self, clock, discovery, multi):
        discovery.side_effect = [discovery_page(1, page, 1800000000)
                                 for page in range(1, 4)]
        established = ["3" * 31 + BASE58[i] for i in range(45)]
        starters = ["5" * 31 + BASE58[i] for i in range(60)]
        with connect(":memory:") as db:
            add_snapshots(db, [
                Snapshot(pool=pool, token="4" * 32, observed_at=t, price_usd=1,
                         liquidity_usd=1000, source="geckoterminal")
                for pool in established for t in (1799990000, 1799995000)
            ] + [
                Snapshot(pool=pool, token="4" * 32, observed_at=1799998000,
                         price_usd=1, liquidity_usd=1000, source="geckoterminal")
                for pool in starters
            ])
            self.assertEqual(collect_once(db, max_tracked=40,
                                          max_enrich=0, new_pages=3), (60, 60, 0, 0))
        refreshed = [pool for call in multi.call_args_list for pool in call.args[0]]
        self.assertEqual(len(refreshed), 40)
        self.assertEqual(len(set(refreshed) & set(established)), 30)
        self.assertEqual(len(set(refreshed) & set(starters)), 10)
        self.assertTrue(all(len(call.args[0]) <= 20 for call in multi.call_args_list))
        self.assertEqual(discovery.call_count, 3)


    @patch("launchlab.cli.fetch_pools_by_address", return_value=[])
    @patch("launchlab.cli.fetch_new_pools", return_value=[])
    @patch("launchlab.cli.time.time")
    def test_failed_pool_reads_rotate_fairly_across_restarts(self, clock, discovery, multi):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "market.sqlite"
            with connect(path) as db:
                add_snapshots(db, [
                    Snapshot(pool=char * 32, token="4" * 32, observed_at=1799999000,
                             price_usd=1, liquidity_usd=1000, source="geckoterminal")
                    for char in ("3", "5", "7")
                ])
            for attempt in range(3):
                clock.return_value = 1800000000 + attempt * 90
                with connect(path) as db:
                    self.assertEqual(collect_once(db, max_tracked=1,
                                                  max_enrich=0), (0, 0, 0, 0))
            with connect(path) as db:
                self.assertEqual(
                    db.execute("SELECT COUNT(*) FROM refresh_attempts").fetchone()[0], 3)
                self.assertEqual(assess(db)["observations"], 3)
        self.assertEqual([entry.args[0] for entry in multi.call_args_list],
                         [["3" * 32], ["5" * 32], ["7" * 32]])
