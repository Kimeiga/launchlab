"""Small, read-only live GeckoTerminal integration smoke test.

This verifies integration and point-in-time ingestion, not trading profitability.
It intentionally sends no transactions, uses no credentials, and saves no
wallet information. Run only in an environment with outbound network access.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from launchlab.cli import run_report
from launchlab.engine import Config
from launchlab.gecko import fetch_new_pools, fetch_pools_by_address, fetch_token_security
from launchlab.storage import add_securities, add_snapshots, connect

BASE58 = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def valid_solana_address(address: str) -> bool:
    return 20 <= len(address) <= 64 and all(char in BASE58 for char in address)


def verify_live() -> dict:
    """Exercise all three remote endpoints and the SQLite/report pipeline."""
    newly_seen = fetch_new_pools()
    if not newly_seen:
        raise RuntimeError("GeckoTerminal new_pools returned no valid Solana snapshots")

    eligible = [s for s in newly_seen
                if valid_solana_address(s.pool) and valid_solana_address(s.token)]
    if not eligible:
        raise RuntimeError("new_pools returned no usable Solana mint/pool addresses")

    pool_addresses = list(dict.fromkeys(s.pool for s in eligible))[:3]
    refreshed = fetch_pools_by_address(pool_addresses)
    refreshed = [s for s in refreshed if s.pool in pool_addresses]
    if not refreshed:
        raise RuntimeError("pools/multi did not return any requested valid pool quotes")

    # Newly created tokens may not have indexed metadata yet (404), so verify
    # token-info using a long-established Solana mint. This does NOT certify
    # that fresh launches have usable security observations.
    token = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # USDC
    security = fetch_token_security(token)
    if security.token != token or security.source != "geckoterminal_token_info":
        raise RuntimeError("token info normalization returned mismatched metadata")
    security.validate()

    with tempfile.TemporaryDirectory(prefix="launchlab-live-") as folder:
        folder = Path(folder)
        with connect(folder / "observations.sqlite") as db:
            stored_new = add_snapshots(db, newly_seen)
            stored_refresh = add_snapshots(db, refreshed)
            stored_security = add_securities(db, [security])
            result = run_report(db, str(folder / "report.json"),
                                str(folder / "report.html"), Config())
        if not (folder / "report.json").is_file() or not (folder / "report.html").is_file():
            raise RuntimeError("Report outputs missing after live ingestion")
        if result["data_mode"] != "observed_or_imported" or result["coverage"]["snapshots"] < 1:
            raise RuntimeError("Live observations were not retained in the report")

    return {
        "test": "read_only_geckoterminal_smoke",
        "result": "passed",
        "verified": ["new_pools", "pools/multi", "tokens/{mint}/info",
                     "sqlite_ingestion", "report_generation"],
        "discovered_pools": len(newly_seen),
        "refreshed_pools": len(refreshed),
        "stored_new": stored_new,
        "stored_refreshed": stored_refresh,
        "stored_security": stored_security,
        "security_fields_available": {
            "mint_revoked": security.mint_revoked is not None,
            "freeze_revoked": security.freeze_revoked is not None,
            "top10_pct": security.top10_pct is not None,
        },
        "coverage": result["coverage"],
        "note": "Integration smoke only. No profitability evaluation or trades.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, help="Write a JSON summary for CI artifacts")
    args = parser.parse_args()
    summary = verify_live()
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("## LaunchLab live GeckoTerminal smoke\n\n")
            handle.write(f"- New pools: {summary['discovered_pools']}\n")
            handle.write(f"- Refreshed pools: {summary['refreshed_pools']}\n")
            handle.write(f"- Security fields available: {summary['security_fields_available']}\n")
            handle.write("- Integration-only check; no real trades or returns tested.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
