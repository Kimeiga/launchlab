# LaunchLab

**Research-only Solana launch scanner and paper-strategy evaluator (v0.2)**. It never
connects to a wallet, signs a transaction, or places an order.

This is a first implementation of the question: *Do rule-based token filters
and momentum selection outperform simple controls, after modeled execution costs?*

## Quick start (Python 3.10+, standard library only)

```bash
# Work from the project directory; no package installation required.
python -m launchlab --db data/demo.sqlite demo --html outputs/demo.html --json outputs/demo.json
# Open outputs/demo.html in a browser.

# Live market observations, read-only, no API key required:
python -m launchlab --db data/market.sqlite collect
python -m launchlab --db data/market.sqlite watch --interval 90

# Add actual, contemporaneous security observations from a verified source:
python -m launchlab --db data/market.sqlite import-security examples/security_template.csv

# Evaluate; absent security evidence fails closed:
python -m launchlab --db data/market.sqlite report --html outputs/report.html --json outputs/report.json

# Unit and integration tests:
python -m unittest discover -s tests -v
```

**Warning:** The `demo` command generates *fabricated* market observations and
fabricated risk findings for interface/algorithm testing. It is not an empirical
trading result. Do not mix the synthetic database with live observations.

## What it implements

- The documented GeckoTerminal Public API: `GET /networks/solana/new_pools`
  and `GET /networks/solana/pools/multi/{addresses}`.
  - Pool discovery, repeated sampling, provider rate-limit retry/backoff,
    SQLite storage, timestamps at ingestion. Discovery fetches only the
    first page, and refreshing is capped, so it is NOT a complete launch
    universe and must not be used to estimate population-level success rates.
  - GeckoTerminal token info for up to 2 candidates per collection cycle (configurable),
    attempting mint/freeze flags and top-ten token-account concentration only
    when the source fields are present and holder data is recent. Missing/stale
    fields stay unknown. This is not wallet-cluster-aware ownership analysis.
  - No private keys or API credentials.
- Importable security observations, valid only **at or before** decision time.
  - Mint and freeze authority revoked, top-ten concentration ≤25%, pool
    liquidity ≥$40k. Security evidence older than 30 minutes is rejected. Missing information blocks the pool.
  - These are initial heuristic cutoffs, not validated optima. Token info is
    an independent data-source observation, **not** a verified security audit.
    `--max-enrich 0` disables automated token-info retrieval. Enrichment can
    fail on freshly launched tokens, leaving them ineligible.
- Exploratory five-minute momentum rule: sufficient volume, >=12 buy
  transactions, buy fraction ≥65%, price appreciation in a bounded range.
  Transaction counts do not prove buyers are independent or organic.
- Three **paper** strategies:
  - `momentum`: highest-scoring safety-passing candidate each active minute.
  - `random_at_signal_times`: random eligible candidate at the same moments
    the momentum arm has at least one candidate. Tests candidate *selection*,
    not signal timing.
  - `safety_only`: highest-liquidity safe candidate every active minute, as a
    diagnostic; it isn't trade-count matched.
- Orders are entered and exited no sooner than the **next subsequent sampled
  observation**, never on the signal quote. Delayed fills, unfilled requests,
  $0.35 per-side fee floor, percentage platform fees, pool fees, conservative
  TVL-based constant-product price impact estimate, additional slippage,
  stop-losses, trailing stops, position limits, and max holding times.
- JSON and self-contained HTML with trade log, drawdown, marked equity,
  baseline comparisons, source timestamps and excluded-pool counts.

## Data contracts

`import-snapshots`: CSV headings (prices and timestamps required):

```csv
pool,token,observed_at,price_usd,liquidity_usd,pool_name,created_at,volume_m5_usd,buys_m5,sells_m5,buyers_m5,price_change_m5_pct,source
```

`import-security`:

```csv
token,observed_at,mint_revoked,freeze_revoked,top10_pct,source,holder_updated_at
```

- All timestamps are **Unix UTC seconds**. `observed_at` must be when the
  reading was actually available. Do **not** use today's security result for
  yesterday's backtest unless it was historically observed then.
- `holder_updated_at` is optional for manually sourced records. Set it when
  the provider reports a separate timestamp for the underlying holder data.
  An absent timestamp on manual imports means freshness is assessed from
  `observed_at`; don't present old holder data as newly observed.
- `top10_pct` must measure the same beneficial-ownership concept across
  tokens. A raw top-ten holder count can include the pool or system-owned
  accounts and can miss linked wallets; record the source and methodology.
- `mint_revoked=true` and `freeze_revoked=true` mean the relevant Solana
  authorities are disabled. Do not use a generic numeric risk score as if
  it proved these flags.
- The simulator rejects mixing synthetic and observed records even if they
  bypass the SQLite importer. Existing v0.1 SQLite databases are migrated
  non-destructively to store holder-update timestamps.
- Provider snapshots are cached and incomplete. An API request failing is
  not the same as a valid zero reading. Cached price data cannot validate
  execution latency or intra-minute high/low stops.

## Explicit limitations

This is a **forward observation research instrument**, not a validated
backtester for historical token launches. It lacks:

1. A historical point-in-time universe of *all* Solana launchpad pools,
   including delisted/collapsed tokens (survivorship-bias control).
2. Automatically sourced mint/freeze and cluster-adjusted ownership
   observations with reliable as-of timestamps.
3. Transaction-level swaps and historical pool reserves, complete buy/sell
   execution fidelity, failed swaps, MEV, dynamic priority fees, routing,
   concentrated liquidity, manipulated volumes and malicious tokens.
4. Independent holdout evaluation, confidence intervals, and enough trades to
   establish a statistically meaningful edge. Three strategies are hypotheses.

The price impact estimate treats half of reported pool liquidity as the USD
quote reserve in a constant-product pool. This can be substantially wrong for
real pools. Marked account equity includes estimated liquidation proceeds for
fresh quotes, but **marks stale open positions at zero**. Stops trigger on
sampled prices and only fill if a later quote exists: a configured stop is
not a guaranteed loss bound.

Source references:
- GeckoTerminal API: https://api.geckoterminal.com/docs/index.html
- GeckoTerminal v2 OpenAPI: https://api.geckoterminal.com/docs/v2/swagger.json
- Solana token authorities and RugCheck: https://solana.com/docs/tokens/how-to-verify-a-token
- DEX Screener reference (potential future alternative): https://docs.dexscreener.com/api/reference

## Security

Read-only network GETs to a fixed GeckoTerminal host. SQLite and local HTML
are the only outputs. No wallet/seed collection, auto-trading, network POSTs,
telemetry, CDN scripts, or remote assets.

## Verification status

- 36 offline automated tests cover the simulator, ingestion, report generation,
  provider parsing, fill-time checks, token identity, future timestamps, and
  backwards-compatible SQLite migration.
- **Real GeckoTerminal GET requests could not be exercised in the artifact
  building environment because its external network access was unavailable.**
  The public API integration therefore needs a live smoke test before relying
  on unattended collection. Endpoint and field names were checked against
  GeckoTerminal's published API documentation.
- The HTML demo contains only synthetic inputs and illustrative output.
