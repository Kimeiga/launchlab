from __future__ import annotations

import sqlite3
from pathlib import Path
from .models import SecurityObservation, Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    pool TEXT NOT NULL,
    token TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    price_usd REAL NOT NULL,
    liquidity_usd REAL NOT NULL,
    pool_name TEXT NOT NULL,
    created_at INTEGER,
    volume_m5_usd REAL,
    buys_m5 INTEGER,
    sells_m5 INTEGER,
    buyers_m5 INTEGER,
    price_change_m5_pct REAL,
    source TEXT NOT NULL,
    PRIMARY KEY (pool, observed_at)
);
CREATE INDEX IF NOT EXISTS snapshots_ts ON snapshots(observed_at);
CREATE INDEX IF NOT EXISTS snapshots_token_ts ON snapshots(token, observed_at);
CREATE TABLE IF NOT EXISTS securities (
    token TEXT NOT NULL,
    observed_at INTEGER NOT NULL,
    mint_revoked INTEGER,
    freeze_revoked INTEGER,
    top10_pct REAL,
    source TEXT NOT NULL,
    holder_updated_at INTEGER,
    PRIMARY KEY(token, observed_at)
);
CREATE INDEX IF NOT EXISTS securities_ts ON securities(observed_at);
CREATE TABLE IF NOT EXISTS refresh_attempts (
    pool TEXT PRIMARY KEY,
    attempted_at INTEGER NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    # Read databases created by the original v0.1 schema without data loss.
    columns = {row[1] for row in db.execute("PRAGMA table_info(securities)")}
    if "holder_updated_at" not in columns:
        db.execute("ALTER TABLE securities ADD COLUMN holder_updated_at INTEGER")
        db.commit()
    return db


def add_snapshots(db: sqlite3.Connection, snapshots: list[Snapshot]) -> int:
    for s in snapshots:
        s.validate()
    incoming_synthetic = {s.source == "synthetic" for s in snapshots}
    if len(incoming_synthetic) > 1:
        raise ValueError("Cannot mix synthetic and real observations in one import")
    existing_sources = {r[0] == "synthetic" for r in db.execute("SELECT DISTINCT source FROM snapshots")}
    existing_security_sources = {r[0] == "synthetic" for r in db.execute("SELECT DISTINCT source FROM securities")}
    if incoming_synthetic and (existing_sources | existing_security_sources) and incoming_synthetic != (existing_sources | existing_security_sources):
        raise ValueError("Cannot mix synthetic and real observations in one database")
    before = db.total_changes
    with db:
        db.executemany(
            """INSERT OR IGNORE INTO snapshots
            (pool,token,observed_at,price_usd,liquidity_usd,pool_name,created_at,
             volume_m5_usd,buys_m5,sells_m5,buyers_m5,price_change_m5_pct,source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(s.pool,s.token,s.observed_at,s.price_usd,s.liquidity_usd,s.pool_name,
              s.created_at,s.volume_m5_usd,s.buys_m5,s.sells_m5,s.buyers_m5,
              s.price_change_m5_pct,s.source) for s in snapshots],
        )
    return db.total_changes - before


def add_securities(db: sqlite3.Connection, securities: list[SecurityObservation]) -> int:
    for s in securities:
        s.validate()
    incoming_synthetic = {s.source == "synthetic" for s in securities}
    if len(incoming_synthetic) > 1:
        raise ValueError("Cannot mix synthetic and real security observations")
    existing_sources = {r[0] == "synthetic" for r in db.execute("SELECT DISTINCT source FROM snapshots")}
    existing_security_sources = {r[0] == "synthetic" for r in db.execute("SELECT DISTINCT source FROM securities")}
    if incoming_synthetic and (existing_sources | existing_security_sources) and incoming_synthetic != (existing_sources | existing_security_sources):
        raise ValueError("Cannot mix synthetic and real observations in one database")
    before = db.total_changes
    with db:
        db.executemany(
            """INSERT OR IGNORE INTO securities
            (token,observed_at,mint_revoked,freeze_revoked,top10_pct,source,holder_updated_at)
            VALUES (?,?,?,?,?,?,?)""",
            [(s.token, s.observed_at, s.mint_revoked, s.freeze_revoked, s.top10_pct, s.source,
              s.holder_updated_at)
             for s in securities],
        )
    return db.total_changes - before


def snapshots_all(db: sqlite3.Connection) -> list[Snapshot]:
    return [Snapshot(**dict(row)) for row in db.execute("SELECT * FROM snapshots ORDER BY observed_at,pool")]


def securities_all(db: sqlite3.Connection) -> list[SecurityObservation]:
    return [SecurityObservation(token=r['token'], observed_at=r['observed_at'],
            mint_revoked=None if r['mint_revoked'] is None else bool(r['mint_revoked']),
            freeze_revoked=None if r['freeze_revoked'] is None else bool(r['freeze_revoked']),
            top10_pct=r['top10_pct'], source=r['source'], holder_updated_at=r['holder_updated_at'])
            for r in db.execute("SELECT * FROM securities ORDER BY observed_at,token")]


def latest_snapshots(db: sqlite3.Connection, limit: int = 200) -> list[Snapshot]:
    rows = db.execute("""SELECT s.* FROM snapshots s
        JOIN (SELECT pool, MAX(observed_at) AS t FROM snapshots GROUP BY pool) x
        ON s.pool=x.pool AND s.observed_at=x.t
        ORDER BY s.observed_at DESC, s.pool LIMIT ?""", (limit,))
    return [Snapshot(**dict(row)) for row in rows]


def select_refresh_candidates(
    db: sqlite3.Connection, *, since: int, until: int,
    exclude_pools: set[str], limit: int,
) -> list[str]:
    """Choose recent, previously observed GeckoTerminal pools for follow-up.

    A quarter of capacity admits single-observation pools when both groups
    exist; the rest maintains pools with an established price series. Unused
    slots are borrowed by the other group. Within each group the least recently
    *attempted* pools go first, including when a prior request returned no data.
    """
    if limit <= 0:
        return []
    rows = db.execute(
        """SELECT s.pool, COUNT(*) AS observation_count,
                  COALESCE(MAX(a.attempted_at), 0) AS last_attempted_at
           FROM snapshots AS s
           LEFT JOIN refresh_attempts AS a ON a.pool = s.pool
           WHERE s.source = 'geckoterminal'
           GROUP BY s.pool
           HAVING MAX(s.observed_at) BETWEEN ? AND ?
           ORDER BY last_attempted_at ASC, MAX(s.observed_at) ASC, s.pool ASC""",
        (since, until),
    )
    established: list[str] = []
    starters: list[str] = []
    for row in rows:
        if row["pool"] in exclude_pools:
            continue  # Already sampled by discovery this cycle.
        (established if row["observation_count"] >= 2 else starters).append(row["pool"])

    starter_slots = max(1, (limit + 3) // 4) if limit > 1 else 0
    selected = established[:limit - starter_slots] + starters[:starter_slots]
    if len(selected) < limit:
        selected_set = set(selected)
        for pool in established + starters:
            if pool not in selected_set:
                selected.append(pool)
                selected_set.add(pool)
                if len(selected) == limit:
                    break
    return selected


def record_refresh_attempts(
    db: sqlite3.Connection, pools: list[str], attempted_at: int,
) -> None:
    """Record requests, not observations, so missing pools cannot monopolize slots."""
    if not pools:
        return
    with db:
        db.executemany(
            """INSERT INTO refresh_attempts (pool, attempted_at) VALUES (?, ?)
               ON CONFLICT(pool) DO UPDATE
               SET attempted_at = MAX(refresh_attempts.attempted_at, excluded.attempted_at)""",
            [(pool, attempted_at) for pool in pools],
        )
