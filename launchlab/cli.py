from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import replace
from pathlib import Path
from .demo import make_demo
from .engine import Config, simulate
from .gecko import fetch_new_pools, fetch_pools_by_address, fetch_token_security
from .models import SecurityObservation, Snapshot
from .report import render_report
from .storage import add_securities, add_snapshots, connect, latest_snapshots, securities_all, snapshots_all


def _nullable_number(row: dict[str, str], field: str, kind: type = float):
    val = row.get(field)
    return None if val is None or val.strip() == "" else kind(val)


def _boolean(row: dict[str, str], field: str) -> bool | None:
    val = (row.get(field) or "").strip().lower()
    if val in ("true", "1", "yes", "y"):
        return True
    if val in ("false", "0", "no", "n"):
        return False
    if not val:
        return None
    raise ValueError(f"{field} must be true, false or empty (not {val!r})")


def _import_csv(db, path: str, model: str) -> int:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if model == "security":
        items = [SecurityObservation(token=row["token"], observed_at=int(row["observed_at"]),
                   mint_revoked=_boolean(row, "mint_revoked"),
                   freeze_revoked=_boolean(row, "freeze_revoked"),
                   top10_pct=_nullable_number(row, "top10_pct"),
                   source=row.get("source") or "user_import",
                   holder_updated_at=_nullable_number(row, "holder_updated_at", int)) for row in rows]
        return add_securities(db, items)
    items = [Snapshot(pool=row["pool"], token=row["token"],
                observed_at=int(row["observed_at"]), price_usd=float(row["price_usd"]),
                liquidity_usd=float(row["liquidity_usd"]), pool_name=row.get("pool_name") or "",
                created_at=_nullable_number(row, "created_at", int),
                volume_m5_usd=_nullable_number(row, "volume_m5_usd"),
                buys_m5=_nullable_number(row, "buys_m5", int),
                sells_m5=_nullable_number(row, "sells_m5", int),
                buyers_m5=_nullable_number(row, "buyers_m5", int),
                price_change_m5_pct=_nullable_number(row, "price_change_m5_pct"),
                source=row.get("source") or "user_import") for row in rows]
    return add_snapshots(db, items)


def collect_once(db, max_tracked: int = 40, max_enrich: int = 2) -> tuple[int, int, int, int]:
    if not 0 <= max_tracked <= 100 or not 0 <= max_enrich <= 4:
        raise ValueError("max_tracked must be 0..100 and max_enrich 0..4")
    new = fetch_new_pools()
    added = add_snapshots(db, new)
    # Keep refreshing pools we have actually observed recently: without this,
    # the new-pool discovery endpoint only creates a one-point time series.
    since = int(time.time()) - 24*3600
    known = [s.pool for s in latest_snapshots(db, limit=200)
             if s.source == "geckoterminal" and s.observed_at >= since]
    refresh = known[:max_tracked]
    updated = 0
    for i in range(0, len(refresh), 20):
        batch = fetch_pools_by_address(refresh[i:i+20])
        updated += add_snapshots(db, batch)
    # Request token-info details for a small number of liquid candidates.
    # Provider may not report fresh holder distribution; such evidence remains
    # unknown rather than being turned into a positive risk verdict.
    tokens_to_check = []
    seen = set()
    for s in sorted(latest_snapshots(db, limit=150),
                    key=lambda s: (-s.liquidity_usd, -s.observed_at)):
        if (s.source != "geckoterminal" or s.token in seen or
            s.liquidity_usd < 40_000 or s.observed_at < since):
            continue
        seen.add(s.token)
        recent = db.execute("SELECT MAX(observed_at) FROM securities WHERE token=?",
                            (s.token,)).fetchone()[0]
        if recent is None or recent < int(time.time()) - 15 * 60:
            tokens_to_check.append(s.token)
        if len(tokens_to_check) >= max_enrich:
            break
    enriched = 0
    for token in tokens_to_check[:max_enrich]:
        try:
            enriched += add_securities(db, [fetch_token_security(token)])
        except (RuntimeError, ValueError) as error:
            print(f"Security info unavailable for {token[:10]}…: {error}", file=sys.stderr)
    return len(new), added, updated, enriched


def run_report(db, json_path: str, html_path: str, cfg: Config) -> dict:
    snapshots = snapshots_all(db)
    sec = securities_all(db)
    results = simulate(snapshots, sec, cfg)
    json_file = Path(json_path)
    html_file = Path(html_path)
    json_file.parent.mkdir(parents=True, exist_ok=True)
    html_file.parent.mkdir(parents=True, exist_ok=True)
    json_file.write_text(json.dumps(results, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    html_file.write_text(render_report(results, snapshots, sec), encoding="utf-8")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="launchlab", description="Read-only Solana pool signal research")
    parser.add_argument("--db", default="data/launchlab.sqlite", help="SQLite file (default data/launchlab.sqlite)")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="Run on explicitly synthetic data; no network required")
    demo.add_argument("--json", default="outputs/demo.json")
    demo.add_argument("--html", default="outputs/demo.html")
    collect = sub.add_parser("collect", help="Get new pools and refresh recent pools once")
    collect.add_argument("--max-tracked", type=int, default=40)
    collect.add_argument("--max-enrich", type=int, default=2)
    watch = sub.add_parser("watch", help="Observe new pools periodically; no trades")
    watch.add_argument("--interval", type=int, default=90)
    watch.add_argument("--max-tracked", type=int, default=40)
    watch.add_argument("--max-enrich", type=int, default=2)
    snap = sub.add_parser("import-snapshots", help="Import timestamped observations from CSV")
    snap.add_argument("csv_file")
    security = sub.add_parser("import-security", help="Import as-of security observations from CSV")
    security.add_argument("csv_file")
    report = sub.add_parser("report", help="Write a self-contained HTML report and machine-readable JSON")
    report.add_argument("--json", default="outputs/report.json")
    report.add_argument("--html", default="outputs/report.html")
    report.add_argument("--initial-cash", type=float, default=150)
    report.add_argument("--position-fraction", type=float, default=0.08)
    report.add_argument("--seed", type=int, default=47)
    args = parser.parse_args(argv)
    try:
        with connect(args.db) as db:
            if args.command == "demo":
                snapshots, securities = make_demo()
                add_snapshots(db, snapshots)
                add_securities(db, securities)
                results = run_report(db, args.json, args.html, Config())
                print(f"Synthetic demo only: {len(snapshots)} fabricated observations")
                print(f"HTML: {Path(args.html).resolve()}\nJSON: {Path(args.json).resolve()}")
                return 0
            if args.command == "collect":
                new, added, refreshed, enriched = collect_once(db, args.max_tracked, args.max_enrich)
                print(f"Discovered {new} pools; stored {added} new records, {refreshed} refreshed records, {enriched} security observations.")
                print("Token-info evidence is provider-dependent. Missing/stale security fields fail closed.")
                return 0
            if args.command == "watch":
                if args.interval < 60:
                    parser.error("watch interval must be >=60 seconds to respect cached data and API limits")
                print(f"Collecting read-only data every {args.interval}s. Ctrl+C to stop.", flush=True)
                try:
                    while True:
                        started = time.monotonic()
                        try:
                            n, a, r, e = collect_once(db, args.max_tracked, args.max_enrich)
                            print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}: {n} discovered, {a} new, {r} refreshed, {e} security", flush=True)
                        except RuntimeError as error:
                            print(f"Read error (will retry next cycle): {error}", file=sys.stderr, flush=True)
                        time.sleep(max(0, args.interval - (time.monotonic() - started)))
                except KeyboardInterrupt:
                    print("\nStopped. Existing observations are preserved.")
                return 0
            if args.command in ("import-snapshots", "import-security"):
                n = _import_csv(db, args.csv_file, "security" if args.command == "import-security" else "snapshots")
                print(f"Imported {n} observations (duplicates ignored).")
                return 0
            if args.command == "report":
                cfg = replace(Config(), initial_cash=args.initial_cash,
                              position_fraction=args.position_fraction, seed=args.seed)
                result = run_report(db, args.json, args.html, cfg)
                print(f"Wrote {args.html} and {args.json}. ")
                for name, s in result["strategies"].items():
                    print(f"{name:24s} return {s['return_pct']:7.2f}% closed={s['closed_trades']} open={s['open_positions']} pending={s['pending_orders']}")
                if not result["coverage"]["security_observations"]:
                    print("No as-of security observations. Fail-closed behavior prevents strategy entries.")
                return 0
    except (ValueError, RuntimeError, OSError, KeyError, csv.Error) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
