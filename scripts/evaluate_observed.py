"""Read-only replay of real, archived point-in-time observations.

This is not a historical launch-universe backtest or evidence of realized returns.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from launchlab.engine import Config, simulate
from launchlab.report import render_report
from launchlab.storage import connect, securities_all, snapshots_all


def timestamp(value: str | None) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Invalid timezone-aware ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError("Window must include a timezone offset or Z")
    return int(parsed.timestamp())


def iso_utc(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def evaluate(db, *, start: int | None = None, end: int | None = None,
             cfg: Config = Config()) -> tuple[dict, dict, list, list]:
    if start is not None and end is not None and start > end:
        raise ValueError("start must not be after end")
    observations = [s for s in snapshots_all(db)
                    if (start is None or s.observed_at >= start)
                    and (end is None or s.observed_at <= end)]
    if not observations:
        raise ValueError("No observations in the requested window")
    if any(s.source != "geckoterminal" for s in observations):
        raise ValueError("Observed replay requires genuine GeckoTerminal snapshots")
    last = max(s.observed_at for s in observations)
    securities = [s for s in securities_all(db) if s.observed_at <= last]
    report = simulate(observations, securities, cfg)
    counts = Counter(s.pool for s in observations)
    per_pool = defaultdict(list)
    for s in observations:
        per_pool[s.pool].append(s.observed_at)
    gaps = sum(b - a > 900 for times in per_pool.values()
               for a, b in zip(sorted(times), sorted(times)[1:]))
    complete = sum(s.mint_revoked is not None and s.freeze_revoked is not None
                   and s.top10_pct is not None for s in securities)
    strategy_fields = ("return_pct", "equity_usd", "closed_pnl_usd", "closed_trades",
                       "open_positions", "pending_orders", "attempted_entries",
                       "expired_orders", "max_drawdown_pct", "platform_network_fees_usd")
    strategies = {name: {key: data[key] for key in strategy_fields}
                  for name, data in report["strategies"].items()}
    completed = sum(s["closed_trades"] for s in strategies.values())
    status = ("no_completed_paper_trades" if completed == 0
              else "exploratory_modeled_outcomes_not_validated")
    verdict = ("No completed paper trades: a zero return cannot establish an edge or lack of one."
               if completed == 0 else
               "Modeled historical outcomes are exploratory, not demonstrated profitability.")
    summary = {
        "schema_version": 1,
        "source": "archived_as_observed_geckoterminal_quotes",
        "period_utc": {"start": iso_utc(min(s.observed_at for s in observations)),
                       "end": iso_utc(last)},
        "observations": len(observations),
        "pools": len(counts),
        "pools_with_2plus_quotes": sum(n >= 2 for n in counts.values()),
        "pools_with_3plus_quotes": sum(n >= 3 for n in counts.values()),
        "gaps_over_15_minutes": gaps,
        "security_observations_as_of_end": len(securities),
        "complete_security_observations_as_of_end": complete,
        "minutes_with_safe_candidates": report["coverage"]["minutes_with_safe_candidates"],
        "minutes_with_momentum_candidates": report["coverage"]["minutes_with_momentum_candidates"],
        "rejections": report["coverage"]["rejections_on_new_quotes"],
        "strategies": strategies,
        "evidence_status": status,
        "conclusion": verdict,
        "limitations": [
            "Not a complete historical pool universe; only actually collected quotes count.",
            "Quotes can be cached, and gaps are not interpolated.",
            "Missing/stale point-in-time security evidence prevents entries.",
            "Next-observation fills and fee/impact models are not actual executions.",
            "Results do not control survivorship bias or establish statistical significance.",
        ],
    }
    return summary, report, observations, securities


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/market.sqlite")
    parser.add_argument("--start", help="Optional timezone-aware ISO-8601 UTC window start")
    parser.add_argument("--end", help="Optional timezone-aware ISO-8601 UTC window end")
    parser.add_argument("--summary", default="outputs/observed-summary.json")
    parser.add_argument("--json", default="outputs/observed-report.json")
    parser.add_argument("--html", default="outputs/observed-report.html")
    parser.add_argument("--initial-cash", type=float, default=150)
    parser.add_argument("--position-fraction", type=float, default=.08)
    args = parser.parse_args(argv)
    try:
        cfg = replace(Config(), initial_cash=args.initial_cash,
                      position_fraction=args.position_fraction)
        with connect(args.db) as db:
            summary, report, observations, securities = evaluate(
                db, start=timestamp(args.start), end=timestamp(args.end), cfg=cfg)
        for dest in (args.summary, args.json, args.html):
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(args.summary).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        Path(args.json).write_text(json.dumps(report, indent=2, allow_nan=False) + "\n",
                                   encoding="utf-8")
        Path(args.html).write_text(render_report(report, observations, securities),
                                   encoding="utf-8")
        print(json.dumps(summary, indent=2))
        lines = ["## LaunchLab observed-only paper replay", "",
                 f"Window: {summary['period_utc']['start']} to {summary['period_utc']['end']}",
                 f"Pools: {summary['pools']}, observations: {summary['observations']}, "
                 f"pools with 3+ quotes: {summary['pools_with_3plus_quotes']}",
                 f"Complete security observations: {summary['complete_security_observations_as_of_end']}",
                 f"**{summary['conclusion']}**", "",
                 "| Strategy | Closed trades | Modeled return | Closed P&L USD |",
                 "|---|---:|---:|---:|"]
        for name, stats in summary["strategies"].items():
            lines.append(f"| {name} | {stats['closed_trades']} | "
                         f"{stats['return_pct']:.2f}% | {stats['closed_pnl_usd']:.2f} |")
        lines.append("\nNo trading, no historical-universe reconstruction, no profitability guarantee.")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
    except (ValueError, RuntimeError, OSError) as error:
        print(f"Unable to replay observations: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
