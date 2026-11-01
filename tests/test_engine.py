import unittest
from dataclasses import replace
from launchlab.demo import make_demo
from launchlab.engine import Config, _buy, _sell, momentum_score, security_reason, simulate
from launchlab.models import SecurityObservation, Snapshot
from launchlab.storage import connect, add_snapshots, add_securities, snapshots_all

T = 1_800_000_000
CFG = Config()


def snapshot(t=T, pool='pool_alpha', token='mint_alpha', price=.0001, liq=50000,
             buys=25, sells=5, volume=5000, pct=5):
    return Snapshot(pool=pool, token=token, observed_at=t, price_usd=price,
                    liquidity_usd=liq, buys_m5=buys, sells_m5=sells,
                    volume_m5_usd=volume, price_change_m5_pct=pct,
                    source="test")


def security(t=T-60, token='mint_alpha', mint=True, freeze=True, top=12):
    return SecurityObservation(token=token, observed_at=t,
                    mint_revoked=mint, freeze_revoked=freeze, top10_pct=top)


class EngineTests(unittest.TestCase):
    def test_missing_security_fails_closed(self):
        s = snapshot()
        self.assertEqual(security_reason(s, None, CFG), 'unknown_security')
        self.assertEqual(security_reason(s, security(mint=None), CFG), 'unknown_security')
        self.assertEqual(security_reason(s, security(top=45), CFG), 'concentrated_holders')
        self.assertEqual(security_reason(s, security(freeze=False), CFG), 'active_authority')
        self.assertIsNone(security_reason(s, security(), CFG))

    def test_security_data_from_future_must_not_pass(self):
        observed = [snapshot(T), snapshot(T+60), snapshot(T+120)]
        results = simulate(observed, [security(t=T+180)])
        self.assertEqual(results['strategies']['momentum']['attempted_entries'], 0)
        self.assertEqual(results['strategies']['momentum']['closed_trades'], 0)
        self.assertEqual(results['coverage']['rejections_on_new_quotes']['unknown_security'], 3)

    def test_signal_does_not_fill_at_same_observation(self):
        observed = [snapshot(T), snapshot(T+60), snapshot(T+120)]
        results = simulate(observed, [security()])['strategies']['momentum']
        buys = [t for t in results['trades'] if t['kind'] == 'buy']
        self.assertEqual(len(buys), 1)
        self.assertEqual(buys[0]['time'], T+60)

    def test_stop_fills_at_next_quote_and_gap_can_exceed_six_percent(self):
        observed = [snapshot(T), snapshot(T+60, price=.0001),
                    snapshot(T+120, price=.000091), # stop observed, request to sell
                    snapshot(T+180, price=.000075)] # actual sell fill
        result = simulate(observed, [security()])['strategies']['momentum']
        exits = [t for t in result['trades'] if t['kind'] == 'sell']
        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]['time'], T+180)
        self.assertEqual(exits[0]['reason'], 'stop_loss')
        self.assertLess(exits[0]['pnl_usd'], -.06 * CFG.initial_cash * CFG.position_fraction)

    def test_no_future_quote_means_no_guaranteed_stop_fill(self):
        observed = [snapshot(T),snapshot(T+60),snapshot(T+120, price=.000089)]
        result = simulate(observed, [security()])['strategies']['momentum']
        self.assertEqual(result['closed_trades'], 0)
        self.assertEqual(result['open_positions'], 1)
        self.assertEqual(result['pending_orders'], 1)
        self.assertEqual(result['stop_orders'], 1)

    def test_minimum_platform_fees_reduce_position_value(self):
        entry = _buy(12.0, snapshot(), CFG)
        self.assertIsNotNone(entry)
        qty, fees, impact, price = entry
        self.assertAlmostEqual(fees, 0.353, places=6)
        self.assertLess(qty * .0001, 11.65)
        cash, sell_fees, _ = _sell(qty, snapshot(), CFG)
        self.assertAlmostEqual(sell_fees, 0.353, places=6)
        self.assertLess(cash, 11.65)
        self.assertLess(cash, 12.0)

    def test_price_impact_increases_with_order_size(self):
        a = _buy(10, snapshot(liq=50000), CFG)
        b = _buy(100, snapshot(liq=50000), CFG)
        self.assertLess(a[2]/10, b[2]/100)
        self.assertIsNone(_buy(3000, snapshot(liq=50000), CFG))

    def test_deterministic_control(self):
        snaps, securities = make_demo()
        a = simulate(snaps, securities)
        b = simulate(snaps, securities)
        self.assertEqual(a, b)
        self.assertEqual(a['data_mode'], 'synthetic')
        self.assertGreater(a['coverage']['minutes_with_momentum_candidates'], 0)
        self.assertGreater(a['strategies']['momentum']['closed_trades'], 0)
        self.assertEqual(a['strategies']['momentum']['pending_orders'], 0)

    def test_missing_momentum_fields_are_not_interpreted_as_zeros(self):
        self.assertIsNone(momentum_score(snapshot(buys=None), CFG))
        self.assertIsNone(momentum_score(snapshot(volume=None), CFG))
        self.assertIsNone(momentum_score(snapshot(pct=None), CFG))
        self.assertIsNotNone(momentum_score(snapshot(), CFG))

    def test_stale_holdings_marked_to_zero(self):
        snaps = [snapshot(T),snapshot(T+60)]
        # Add an unrelated pool at T+600 so the account advances without
        # an updated price. The held asset becomes stale and marks at $0.
        snaps.append(snapshot(T+600, 'pool_other', 'mint_other'))
        result = simulate(snaps, [security(),security(token='mint_other')])['strategies']['momentum']
        self.assertEqual(result['open_positions'], 1)
        self.assertLess(result['equity_usd'], CFG.initial_cash - 10)

    def test_insert_duplicate_snapshot_is_idempotent(self):
        db = connect(':memory:')
        self.assertEqual(add_snapshots(db, [snapshot()]), 1)
        self.assertEqual(add_snapshots(db, [snapshot()]), 0)
        self.assertEqual(len(snapshots_all(db)), 1)
        self.assertEqual(add_securities(db, [security()]), 1)
        self.assertEqual(add_securities(db, [security()]), 0)
        db.close()


if __name__ == '__main__':
    unittest.main()


class DataIntegrityTests(unittest.TestCase):
    def test_no_mixing_synthetic_and_live_observations(self):
        db = connect(':memory:')
        sample, secs = make_demo()
        add_snapshots(db, sample[:1])
        with self.assertRaises(ValueError):
            add_snapshots(db, [snapshot()])
        with self.assertRaises(ValueError):
            add_securities(db, [security()])
        db.close()

    def test_stale_security_never_passes(self):
        sec = security(T-10000)
        self.assertEqual(security_reason(snapshot(), sec, CFG), 'stale_security')

    def test_missing_future_security_unchanged_when_as_of_time_advances(self):
        a = snapshot(T)
        sec = security(T+60)
        self.assertIsNone(security_reason(a, sec, CFG, now=T+60))
