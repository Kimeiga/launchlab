"""Conservative event-driven paper simulation over time-stamped observations.

Not a DEX execution emulator: pool reserve proxy approximates constant-product
price impact, and minute-level quotes cannot simulate sub-second fills, MEV or
failure-to-sell faithfully. Results are hypotheses, not live P&L.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from dataclasses import asdict, dataclass
from math import sqrt
from random import Random
from typing import Any

from .models import SecurityObservation, Snapshot


@dataclass(frozen=True)
class Config:
    initial_cash: float = 150.0
    position_fraction: float = 0.08
    max_positions: int = 3
    min_liquidity_usd: float = 40_000.0
    max_top10_pct: float = 25.0
    security_max_age_seconds: int = 30 * 60
    stop_loss_fraction: float = 0.06
    trailing_fraction: float = 0.12
    max_hold_seconds: int = 90 * 60
    stale_after_seconds: int = 5 * 60
    max_order_wait_seconds: int = 5 * 60
    cooldown_seconds: int = 30 * 60
    min_buy_transactions_m5: int = 12
    min_buy_fraction_m5: float = 0.65
    min_volume_m5_usd: float = 2_000.0
    min_price_change_m5_pct: float = 1.0
    max_price_change_m5_pct: float = 25.0
    platform_fee_fraction: float = 0.01
    platform_min_fee_usd: float = 0.35
    network_fee_usd: float = 0.003
    pool_fee_fraction: float = 0.003
    extra_slippage_fraction: float = 0.005
    max_estimated_price_impact: float = 0.03
    max_entry_price_deviation_fraction: float = 0.10
    seed: int = 47

    def validate(self) -> None:
        if self.initial_cash <= 0 or not 0 < self.position_fraction <= 1 or self.max_positions < 1:
            raise ValueError("invalid starting balance, position fraction, or max_positions")
        for k in ("platform_fee_fraction", "pool_fee_fraction", "extra_slippage_fraction", "stop_loss_fraction", "trailing_fraction", "max_entry_price_deviation_fraction"):
            if not 0 <= getattr(self, k) < 1:
                raise ValueError(f"{k} must be in [0,1)")
        if self.platform_min_fee_usd < 0 or self.network_fee_usd < 0:
            raise ValueError("fees cannot be negative")
        if any(getattr(self, k) <= 0 for k in ("stale_after_seconds", "max_order_wait_seconds", "max_hold_seconds")):
            raise ValueError("invalid time window")


@dataclass
class Order:
    pool: str
    token: str
    created_at: int
    side: str
    reason: str
    amount_usd: float = 0.0
    signal_price_usd: float = 0.0


@dataclass
class Position:
    pool: str
    token: str
    entry_ts: int
    entry_price: float  # effective entry including impact, before fixed platform fees
    paid_usd: float
    tokens: float
    entry_mid: float
    high_mid: float
    last_observed_at: int
    last_price_usd: float
    last_liquidity_usd: float


class SecurityIndex:
    def __init__(self, securities: list[SecurityObservation]):
        self.dates: dict[str, list[int]] = defaultdict(list)
        self.obs: dict[str, list[SecurityObservation]] = defaultdict(list)
        for security in sorted(securities, key=lambda x: (x.token, x.observed_at)):
            self.dates[security.token].append(security.observed_at)
            self.obs[security.token].append(security)

    def at(self, token: str, t: int) -> SecurityObservation | None:
        idx = bisect_right(self.dates.get(token, []), t) - 1
        return self.obs[token][idx] if idx >= 0 else None


def security_reason(s: Snapshot, security: SecurityObservation | None, cfg: Config, *, now: int | None = None) -> str | None:
    if s.liquidity_usd < cfg.min_liquidity_usd:
        return "low_liquidity"
    if security is None or security.mint_revoked is None or security.freeze_revoked is None or security.top10_pct is None:
        return "unknown_security"
    decision_time = now if now is not None else s.observed_at
    # A direct caller may pass future-dated evidence; fail closed regardless.
    if security.observed_at > decision_time:
        return "future_security"
    if decision_time - security.observed_at > cfg.security_max_age_seconds:
        return "stale_security"
    # The provider's holder data can predate the token-info API response.
    if security.holder_updated_at is not None and (
        security.holder_updated_at > security.observed_at or
        decision_time - security.holder_updated_at > cfg.security_max_age_seconds
    ):
        return "stale_holders"
    if not security.mint_revoked or not security.freeze_revoked:
        return "active_authority"
    if security.top10_pct > cfg.max_top10_pct:
        return "concentrated_holders"
    return None


def momentum_score(s: Snapshot, cfg: Config) -> float | None:
    """Exploratory rules on 5m activity; NOT an 'organic trading' detector."""
    if any(v is None for v in (s.buys_m5, s.sells_m5, s.volume_m5_usd, s.price_change_m5_pct)):
        return None
    count = s.buys_m5 + s.sells_m5  # type: ignore[operator]
    if count <= 0:
        return None
    buy_fraction = s.buys_m5 / count  # type: ignore[operator]
    pct = s.price_change_m5_pct  # type: ignore[assignment]
    if (s.buys_m5 < cfg.min_buy_transactions_m5 or buy_fraction < cfg.min_buy_fraction_m5 or
        s.volume_m5_usd < cfg.min_volume_m5_usd or not cfg.min_price_change_m5_pct <= pct <= cfg.max_price_change_m5_pct):
        return None
    # Values are heuristic ranking signals, not statistically calibrated probabilities.
    return round(0.5 * buy_fraction + 0.3 * min(s.volume_m5_usd / max(s.liquidity_usd, 1), 1)
                 + 0.2 * min(pct / cfg.max_price_change_m5_pct, 1), 6)


def _price_impact(mark_usd: float, liquidity_usd: float) -> float:
    """Estimated constant-product impact, using half TVL as quote-side reserve."""
    reserve_quote = liquidity_usd / 2
    return mark_usd / (reserve_quote + mark_usd) if reserve_quote > 0 else 1.0


def _buy(budget: float, s: Snapshot, cfg: Config) -> tuple[float, float, float, float] | None:
    """(tokens, platform+network fees, impact cost proxy, effective fill price).

    budget INCLUDES all fees. Simplified CPMM; actual routing and fees can differ.
    """
    platform_fee = max(budget * cfg.platform_fee_fraction, cfg.platform_min_fee_usd)
    principal = budget - platform_fee - cfg.network_fee_usd
    if principal <= 0 or _price_impact(principal, s.liquidity_usd) > cfg.max_estimated_price_impact:
        return None
    pool_in = principal * (1 - cfg.pool_fee_fraction)
    effective_out = (pool_in / s.price_usd) * (1 - _price_impact(pool_in, s.liquidity_usd))
    tokens = effective_out * (1 - cfg.extra_slippage_fraction)
    if tokens <= 0:
        return None
    impact_cost = pool_in * _price_impact(pool_in, s.liquidity_usd)
    return tokens, platform_fee + cfg.network_fee_usd, impact_cost, principal / tokens


def _sell(tokens: float, s: Snapshot, cfg: Config) -> tuple[float, float, float]:
    """(net proceeds, platform+network fees, price impact cost proxy)."""
    spot_mark = tokens * s.price_usd
    if spot_mark <= 0 or s.liquidity_usd <= 0:
        return 0.0, cfg.network_fee_usd, spot_mark
    pool_out = spot_mark * (1 - cfg.pool_fee_fraction)
    impact = _price_impact(pool_out, s.liquidity_usd)
    quote_out = pool_out * (1 - impact) * (1 - cfg.extra_slippage_fraction)
    fee = max(cfg.platform_min_fee_usd, quote_out * cfg.platform_fee_fraction)
    return max(0.0, quote_out - fee - cfg.network_fee_usd), fee + cfg.network_fee_usd, spot_mark - quote_out


class Portfolio:
    def __init__(self, name: str, cfg: Config):
        self.name = name
        self.cfg = cfg
        self.cash = cfg.initial_cash
        self.positions: dict[str, Position] = {}
        self.pending: dict[str, Order] = {}
        self.cooldown_by_token: dict[str, int] = {}
        self.trades: list[dict[str, Any]] = []
        self.curve: list[dict[str, float | int]] = []
        self.fees: float = 0.0
        self.impact_proxy: float = 0.0
        self.expired_orders = 0
        self.rejected_impact = 0
        self.rejected_safety = 0
        self.rejected_price_deviation = 0
        self.rejected_pool_identity = 0
        self.attempted_entries = 0
        self.stop_orders = 0

    def _request_sell(self, p: Position, now: int, reason: str) -> None:
        if p.pool not in self.pending:
            self.pending[p.pool] = Order(p.pool, p.token, now, "sell", reason)
            if reason in ("stop_loss", "trailing_stop"):
                self.stop_orders += 1

    def on_quotes(self, quotes: dict[str, Snapshot], now: int, security: SecurityIndex) -> None:
        """Fill previously requested orders at subsequent observations only."""
        for pool, order in list(self.pending.items()):
            if now - order.created_at > self.cfg.max_order_wait_seconds:
                del self.pending[pool]
                self.expired_orders += 1
                continue
            quote = quotes.get(pool)
            if quote is None or quote.observed_at <= order.created_at:
                continue
            del self.pending[pool]
            if order.side == "buy":
                self.attempted_entries += 1
                if quote.token != order.token:
                    self.rejected_pool_identity += 1
                    continue
                if len(self.positions) >= self.cfg.max_positions or self.cash < order.amount_usd:
                    continue
                # Don't fill an order if safety evidence expired or deteriorated
                # between the signal and its next executable quote.
                if security_reason(quote, security.at(order.token, quote.observed_at),
                                   self.cfg, now=quote.observed_at) is not None:
                    self.rejected_safety += 1
                    continue
                if (order.signal_price_usd <= 0 or
                    abs(quote.price_usd / order.signal_price_usd - 1)
                    > self.cfg.max_entry_price_deviation_fraction):
                    self.rejected_price_deviation += 1
                    continue
                execution = _buy(order.amount_usd, quote, self.cfg)
                if execution is None:
                    self.rejected_impact += 1
                    continue
                qty, fees, impact, entry_price = execution
                self.cash -= order.amount_usd
                self.fees += fees
                self.impact_proxy += impact
                self.positions[pool] = Position(
                    pool, order.token, quote.observed_at, entry_price,
                    order.amount_usd, qty, quote.price_usd, quote.price_usd,
                    quote.observed_at, quote.price_usd, quote.liquidity_usd,
                )
                self.trades.append({"kind": "buy", "pool": pool, "token": order.token,
                                    "time": quote.observed_at, "price": quote.price_usd,
                                    "amount_usd": -order.amount_usd, "reason": order.reason})
            else:
                p = self.positions.get(pool)
                if p is None:
                    continue
                proceeds, fees, impact = _sell(p.tokens, quote, self.cfg)
                self.cash += proceeds
                self.fees += fees
                self.impact_proxy += impact
                self.cooldown_by_token[p.token] = now
                self.trades.append({"kind": "sell", "pool": pool, "token": p.token,
                                    "time": quote.observed_at, "price": quote.price_usd,
                                    "amount_usd": proceeds, "pnl_usd": round(proceeds - p.paid_usd, 6),
                                    "reason": order.reason})
                del self.positions[pool]

        for pool, p in list(self.positions.items()):
            quote = quotes.get(pool)
            if quote is None or quote.observed_at <= p.last_observed_at:
                continue
            p.last_observed_at = quote.observed_at
            p.last_price_usd = quote.price_usd
            p.last_liquidity_usd = quote.liquidity_usd
            p.high_mid = max(p.high_mid, quote.price_usd)
            if pool in self.pending:
                continue
            # Stop is evaluated on sampled quotes and filled on the *next* quote.
            if quote.price_usd <= p.entry_mid * (1 - self.cfg.stop_loss_fraction):
                self._request_sell(p, now, "stop_loss")
            elif p.high_mid > p.entry_mid and quote.price_usd <= p.high_mid * (1 - self.cfg.trailing_fraction):
                self._request_sell(p, now, "trailing_stop")
            elif now - p.entry_ts >= self.cfg.max_hold_seconds:
                self._request_sell(p, now, "max_hold")

    def can_enter(self, s: Snapshot, now: int) -> bool:
        if len(self.positions) + sum(o.side == "buy" for o in self.pending.values()) >= self.cfg.max_positions:
            return False
        if s.pool in self.pending or s.pool in self.positions:
            return False
        if any(p.token == s.token for p in self.positions.values()):
            return False
        if any(o.token == s.token and o.side == "buy" for o in self.pending.values()):
            return False
        if now - self.cooldown_by_token.get(s.token, -10**15) < self.cfg.cooldown_seconds:
            return False
        budget = self.cash * self.cfg.position_fraction
        fee = max(budget * self.cfg.platform_fee_fraction, self.cfg.platform_min_fee_usd)
        return budget > fee + self.cfg.network_fee_usd

    def enter(self, s: Snapshot, now: int, reason: str) -> bool:
        if not self.can_enter(s, now):
            return False
        budget = self.cash * self.cfg.position_fraction
        self.pending[s.pool] = Order(s.pool, s.token, now, "buy", reason, budget, s.price_usd)
        return True

    def mark_equity(self, now: int) -> None:
        equity = self.cash
        for p in self.positions.values():
            if now - p.last_observed_at <= self.cfg.stale_after_seconds:
                s = Snapshot(p.pool, p.token, p.last_observed_at, p.last_price_usd, p.last_liquidity_usd)
                equity += _sell(p.tokens, s, self.cfg)[0]
            # Stale or missing quotes are zero-valued in the conservative curve.
        self.curve.append({"time": now, "equity": round(equity, 6)})

    def summary(self) -> dict[str, Any]:
        exits = [t for t in self.trades if t["kind"] == "sell"]
        closed_pnl = sum(t["pnl_usd"] for t in exits)
        equity = self.curve[-1]["equity"] if self.curve else self.cfg.initial_cash
        peak = self.cfg.initial_cash
        max_dd = 0.0
        for point in self.curve:
            peak = max(peak, point["equity"])
            max_dd = max(max_dd, 1 - point["equity"] / peak)
        return {
            "name": self.name,
            "equity_usd": round(equity, 4),
            "return_pct": round((equity / self.cfg.initial_cash - 1) * 100, 4),
            "cash_usd": round(self.cash, 4),
            "closed_pnl_usd": round(closed_pnl, 4),
            "closed_trades": len(exits),
            "wins": sum(t["pnl_usd"] > 0 for t in exits),
            "max_drawdown_pct": round(max_dd * 100, 4),
            "platform_network_fees_usd": round(self.fees, 4),
            "pool_impact_loss_proxy_usd": round(self.impact_proxy, 4),
            "open_positions": len(self.positions),
            "pending_orders": len(self.pending),
            "expired_orders": self.expired_orders,
            "stop_orders": self.stop_orders,
            "rejected_price_impact": self.rejected_impact,
            "rejected_safety_at_fill": self.rejected_safety,
            "rejected_entry_price_deviation": self.rejected_price_deviation,
            "rejected_pool_identity": self.rejected_pool_identity,
            "attempted_entries": self.attempted_entries,
            "open_exposure_usd": round(sum(p.paid_usd for p in self.positions.values()), 4),
            "trades": self.trades,
            "curve": self.curve,
            "open": [asdict(p) for p in self.positions.values()],
        }


def simulate(snapshots: list[Snapshot], securities: list[SecurityObservation],
             cfg: Config = Config()) -> dict[str, Any]:
    cfg.validate()
    for s in snapshots:
        s.validate()
    for sec in securities:
        sec.validate()
    if ({s.source == "synthetic" for s in snapshots} |
        {s.source == "synthetic" for s in securities}) == {True, False}:
        raise ValueError("Cannot simulate mixed synthetic and observed data")
    if not snapshots:
        raise ValueError("No snapshots available. Collect or import data first.")
    security = SecurityIndex(securities)
    by_minute: dict[int, dict[str, Snapshot]] = defaultdict(dict)
    for s in sorted(snapshots, key=lambda s: (s.observed_at, s.pool)):
        minute = s.observed_at // 60
        by_minute[minute][s.pool] = s  # last observed pool value in that minute
    rng = Random(cfg.seed)
    strategies = {
        "momentum": Portfolio("momentum", cfg),
        "random_at_signal_times": Portfolio("random_at_signal_times", cfg),
        "safety_only": Portfolio("safety_only", cfg),
    }
    exclusions: dict[str, int] = defaultdict(int)
    decision_minutes = 0
    signal_minutes = 0
    for minute, quotes in sorted(by_minute.items()):
        now = max(s.observed_at for s in quotes.values())
        for p in strategies.values():
            p.on_quotes(quotes, now, security)
        active = list(quotes.values())  # avoid carrying forward stale momentum signals
        passing: list[Snapshot] = []
        momentum: list[tuple[float, Snapshot]] = []
        for s in active:
            reason = security_reason(s, security.at(s.token, now), cfg, now=now)
            if reason:
                if s.pool in quotes:
                    exclusions[reason] += 1
                continue
            passing.append(s)
            score = momentum_score(s, cfg)
            if score is not None:
                momentum.append((score, s))
        if passing:
            decision_minutes += 1
        if momentum:
            signal_minutes += 1
            ranked = sorted(momentum, key=lambda x: (-x[0], x[1].pool))
            for _, s in ranked:
                if strategies["momentum"].enter(s, now, "momentum_signal"):
                    break
            random_candidates = passing[:]
            rng.shuffle(random_candidates)
            for s in random_candidates:
                if strategies["random_at_signal_times"].enter(s, now, "random_control"):
                    break
        # Safety-only arm is a diagnostic: selects the most liquid eligible pool every minute.
        for s in sorted(passing, key=lambda x: (-x.liquidity_usd, x.pool)):
            if strategies["safety_only"].enter(s, now, "safety_only"):
                break
        for p in strategies.values():
            p.mark_equity(now)
    result = {
        "schema_version": 1,
        "data_mode": "synthetic" if all(s.source == "synthetic" for s in snapshots) else "observed_or_imported",
        "config": asdict(cfg),
        "period": {"first_observed_at": min(s.observed_at for s in snapshots),
                   "last_observed_at": max(s.observed_at for s in snapshots)},
        "coverage": {"snapshots": len(snapshots),
                     "pools": len({s.pool for s in snapshots}),
                     "tokens": len({s.token for s in snapshots}),
                     "minutes_with_safe_candidates": decision_minutes,
                     "minutes_with_momentum_candidates": signal_minutes,
                     "security_observations": len(securities),
                     "rejections_on_new_quotes": dict(exclusions)},
        "strategies": {name: p.summary() for name, p in strategies.items()},
        "limitations": [
            "Not independently verified profitability; no actual trades or wallet connection.",
            "Quotes may be cached; execution occurs on the next sampled quote, not sub-second fills.",
            "Queued entries must pass security and price-deviation checks again at the actual fill observation.",
            "CPMM/TVL estimates are only proxies for market impact; concentrated liquidity, routing, MEV, and failures to sell are not faithfully simulated.",
            "Security observations must exist at or before the decision timestamp; no retroactive security leakage.",
            "Stale open holdings are marked at $0; live liquidation value is unknown.",
            "Random baseline shares momentum timing, so it tests selection rather than timing edge.",
            "Pool-level snapshots can duplicate the same underlying token across pools; independent observations are not independent trials.",
        ],
    }
    return result
