"""Regression checks for evidence and execution-time safety gates."""
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from pathlib import Path

from launchlab.engine import Config, security_reason, simulate
from launchlab.gecko import parse_security_info, _timestamp
from launchlab.models import Snapshot, SecurityObservation
from launchlab.storage import connect, add_securities, securities_all

T = 1_800_000_000


def quote(t, *, token='mint', price=0.0001, liq=50_000):
    return Snapshot(pool='pool', token=token, observed_at=t, price_usd=price,
                    liquidity_usd=liq, buys_m5=25, sells_m5=5,
                    volume_m5_usd=5_000, price_change_m5_pct=5)


def safe(t=T-5, *, token='mint', holder_t=None):
    return SecurityObservation(token, t, True, True, 10.0,
                               holder_updated_at=holder_t)


class FillSafetyTests(unittest.TestCase):
    def test_safety_rechecked_at_execution(self):
        snapshots = [quote(T), quote(T+60)]
        securities = [safe(), SecurityObservation('mint', T+30, False, True, 10.0)]
        result = simulate(snapshots, securities)['strategies']['momentum']
        self.assertEqual(result['rejected_safety_at_fill'], 1)
        self.assertEqual(result['open_positions'], 0)
        self.assertEqual(result['trades'], [])

    def test_holder_freshness_rechecked_at_execution(self):
        # Holder data was initially within the 30-minute window, but the
        # provider's underlying data expires before the queued buy can fill.
        snapshots = [quote(T), quote(T+60)]
        result = simulate(snapshots, [safe(T, holder_t=T-1790)])['strategies']['momentum']
        self.assertEqual(result['rejected_safety_at_fill'], 1)
        self.assertEqual(result['open_positions'], 0)

    def test_rejects_large_price_jump_before_fill(self):
        result = simulate([quote(T), quote(T+60, price=.00012)], [safe()])['strategies']['momentum']
        self.assertEqual(result['rejected_entry_price_deviation'], 1)
        self.assertEqual(result['trades'], [])

    def test_rejects_large_price_collapse_before_fill(self):
        result = simulate([quote(T), quote(T+60, price=.00008)], [safe()])['strategies']['momentum']
        self.assertEqual(result['rejected_entry_price_deviation'], 1)

    def test_pool_token_identity_must_match_original_order(self):
        result = simulate([quote(T), quote(T+60, token='newmint')],
                          [safe(), safe(token='newmint')])['strategies']['momentum']
        self.assertEqual(result['rejected_pool_identity'], 1)
        self.assertEqual(result['trades'], [])

    def test_future_security_rejected_even_if_directly_supplied(self):
        self.assertEqual(security_reason(quote(T), safe(T+1), Config()), 'future_security')

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            replace(Config(), max_entry_price_deviation_fraction=1.1).validate()

    def test_mixed_synthetic_and_imported_input_rejected_without_db(self):
        with self.assertRaises(ValueError):
            simulate([replace(quote(T), source='synthetic'), quote(T+60)], [safe()])


class DataMigrationTests(unittest.TestCase):
    def test_old_sqlite_schema_migrates_and_preserves_rows(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / 'old.sqlite'
            raw = sqlite3.connect(path)
            raw.execute('''CREATE TABLE securities (token TEXT NOT NULL, observed_at INTEGER NOT NULL,
              mint_revoked INTEGER, freeze_revoked INTEGER, top10_pct REAL, source TEXT NOT NULL,
              PRIMARY KEY(token,observed_at))''')
            raw.execute("INSERT INTO securities VALUES('old', 1799990000, 1, 1, 10, 'imported')")
            raw.commit()
            raw.close()
            with connect(path) as db:
                self.assertEqual(securities_all(db)[0].holder_updated_at, None)
                self.assertEqual(add_securities(db, [safe(T, holder_t=T-60)]), 1)
                self.assertEqual(securities_all(db)[1].holder_updated_at, T-60)

    def test_naive_provider_timestamp_is_not_treated_as_utc(self):
        self.assertIsNone(_timestamp('2026-11-01T12:00:00'))
        self.assertEqual(_timestamp('2026-11-01T12:00:00Z'),
                         int(datetime(2026, 11, 1, 12, tzinfo=timezone.utc).timestamp()))

    def test_holder_timestamp_cannot_be_after_observation(self):
        with self.assertRaises(ValueError):
            safe(T, holder_t=T+1).validate()

    def test_gecko_holder_as_of_timestamp_survives_normalization(self):
        stamp = datetime.fromtimestamp(T-100, tz=timezone.utc).isoformat()
        parsed = parse_security_info({'data': {'attributes': {
            'address':'mint', 'mint_authority':None, 'freeze_authority':None,
            'holders': {'last_updated':stamp,'distribution_percentage':{'top_10':'12'}}
        }}}, token='mint', observed_at=T)
        self.assertEqual(parsed.holder_updated_at, T-100)
        self.assertEqual(security_reason(quote(T+1750), parsed, Config(), now=T+1750),
                         'stale_holders')


if __name__ == '__main__':
    unittest.main()
