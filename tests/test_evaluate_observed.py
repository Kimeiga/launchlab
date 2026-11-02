"""Offline regressions for honest point-in-time paper-replay reporting."""
import json
import tempfile
import unittest
from pathlib import Path

from launchlab.models import SecurityObservation, Snapshot
from launchlab.storage import add_securities, add_snapshots, connect
from scripts.evaluate_observed import evaluate, main, timestamp


T = 1_800_000_000


def quote(t: int, *, source: str = "geckoterminal") -> Snapshot:
    return Snapshot(pool="3" * 32, token="4" * 32, observed_at=t,
                    price_usd=1 + (t - T) / 6000, liquidity_usd=60_000,
                    buys_m5=25, sells_m5=5, volume_m5_usd=5000,
                    price_change_m5_pct=5, source=source)


class ObservedReplayTests(unittest.TestCase):
    def test_missing_security_means_zero_trades_not_a_break_even_backtest(self):
        with connect(":memory:") as db:
            add_snapshots(db, [quote(T), quote(T + 60), quote(T + 120)])
            summary, report, observations, _ = evaluate(db)
        self.assertEqual(summary["evidence_status"], "no_completed_paper_trades")
        self.assertEqual(summary["pools_with_3plus_quotes"], 1)
        self.assertEqual(summary["complete_security_observations_as_of_end"], 0)
        self.assertEqual(summary["minutes_with_safe_candidates"], 0)
        self.assertEqual(report["strategies"]["momentum"]["closed_trades"], 0)
        self.assertEqual(report["strategies"]["momentum"]["return_pct"], 0)
        self.assertEqual(len(observations), 3)

    def test_future_security_cannot_be_used_to_qualify_historical_signals(self):
        with connect(":memory:") as db:
            add_snapshots(db, [quote(T), quote(T + 60), quote(T + 120),
                               quote(T + 7200)])
            add_securities(db, [SecurityObservation(
                "4" * 32, T + 180, True, True, 10, source="geckoterminal_token_info")])
            summary, report, observations, securities = evaluate(
                db, start=T, end=T + 120)
        self.assertEqual(len(observations), 3)
        self.assertEqual(securities, [])
        self.assertEqual(summary["security_observations_as_of_end"], 0)
        self.assertEqual(report["strategies"]["momentum"]["attempted_entries"], 0)
        self.assertEqual(summary["period_utc"]["end"], "2027-01-15T08:02:00Z")

    def test_synthetic_and_other_imported_prices_are_rejected(self):
        with connect(":memory:") as db:
            add_snapshots(db, [quote(T, source="synthetic")])
            with self.assertRaisesRegex(ValueError, "genuine GeckoTerminal"):
                evaluate(db)
        with connect(":memory:") as db:
            add_snapshots(db, [quote(T, source="user_import")])
            with self.assertRaisesRegex(ValueError, "genuine GeckoTerminal"):
                evaluate(db)

    def test_explicit_utc_bounds_and_empty_window(self):
        self.assertEqual(timestamp("2027-01-15T08:00:00Z"), T)
        with self.assertRaisesRegex(ValueError, "timezone"):
            timestamp("2027-01-15T08:00:00")
        with connect(":memory:") as db:
            add_snapshots(db, [quote(T)])
            with self.assertRaisesRegex(ValueError, "start"):
                evaluate(db, start=T+1, end=T)
            with self.assertRaisesRegex(ValueError, "No observations"):
                evaluate(db, start=T+1, end=T+100)

    def test_cli_writes_summary_and_detailed_report_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            with connect(path / "market.sqlite") as db:
                add_snapshots(db, [quote(T), quote(T + 60)])
            self.assertEqual(main([
                "--db", str(path / "market.sqlite"),
                "--summary", str(path / "summary.json"),
                "--json", str(path / "report.json"),
                "--html", str(path / "report.html"),
            ]), 0)
            summary = json.loads((path / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual(summary["evidence_status"], "no_completed_paper_trades")
            self.assertEqual(json.loads((path / "report.json").read_text(
                encoding="utf-8"))["data_mode"], "observed_or_imported")
            self.assertTrue((path / "report.html").stat().st_size > 100)


if __name__ == "__main__":
    unittest.main()
