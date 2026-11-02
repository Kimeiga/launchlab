"""Audit the observed dataset. Coverage is not population coverage."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from launchlab.storage import connect


def assess(db) -> dict:
    count = lambda sql: int(db.execute(sql).fetchone()[0] or 0)
    totals = db.execute("""SELECT COUNT(*), COUNT(DISTINCT pool), COUNT(DISTINCT token),
                       MIN(observed_at), MAX(observed_at)
                       FROM snapshots""").fetchone()
    dense = db.execute("""SELECT COUNT(*) FROM (
        SELECT pool FROM snapshots GROUP BY pool HAVING COUNT(*) >= 3)""").fetchone()[0]
    gaps = count("""SELECT COUNT(*) FROM (
        SELECT observed_at-LAG(observed_at) OVER (
            PARTITION BY pool ORDER BY observed_at) AS gap FROM snapshots)
        WHERE gap > 900""")
    fields = db.execute("""SELECT COUNT(*),
        SUM(mint_revoked IS NOT NULL), SUM(freeze_revoked IS NOT NULL),
        SUM(top10_pct IS NOT NULL),
        SUM(mint_revoked IS NOT NULL AND freeze_revoked IS NOT NULL AND top10_pct IS NOT NULL),
        COUNT(DISTINCT token) FROM securities""").fetchone()
    newest = db.execute("""SELECT source, COUNT(*) FROM securities
        GROUP BY source ORDER BY source""").fetchall()
    return {
        "observations": int(totals[0]),
        "pools_observed": int(totals[1]),
        "tokens_observed": int(totals[2]),
        "first_observed_at": totals[3],
        "last_observed_at": totals[4],
        "pools_with_at_least_three_observations": int(dense),
        "gaps_over_15_minutes_between_quotes": gaps,
        "security_observations": int(fields[0]),
        "security_tokens_observed": int(fields[5]),
        "security_authority_mint_known": int(fields[1] or 0),
        "security_authority_freeze_known": int(fields[2] or 0),
        "security_top10_raw_accounts_known": int(fields[3] or 0),
        "security_complete_observations": int(fields[4] or 0),
        "security_sources": dict(newest),
        "limitations": [
            "Partial GeckoTerminal discovery pages; not a population-complete launch universe.",
            "Data observed since collection started; historical token lifetimes before start are unknown.",
            "Raw token-account concentration includes pool vaults and is not linked-owner concentration.",
            "Scheduled GitHub Actions can be delayed or skipped; gaps must not be interpolated.",
            "No historical swap-level execution, MEV or failed swap data.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/market.sqlite")
    parser.add_argument("--out", default="outputs/data-quality.json")
    args = parser.parse_args()
    with connect(args.db) as db:
        result = assess(db)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
            handle.write("## LaunchLab point-in-time collection\n\n")
            for key in ("observations", "pools_observed", "pools_with_at_least_three_observations",
                        "security_observations", "security_complete_observations",
                        "gaps_over_15_minutes_between_quotes"):
                handle.write(f"- {key.replace('_', ' ')}: {result[key]}\n")
            handle.write("\n**Not a representative historical token universe or a profitability test.**\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
