from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class Snapshot:
    """Data that was actually observable at observed_at, not retrospectively reconstructed."""
    pool: str
    token: str
    observed_at: int
    price_usd: float
    liquidity_usd: float
    pool_name: str = ""
    created_at: int | None = None
    volume_m5_usd: float | None = None
    buys_m5: int | None = None
    sells_m5: int | None = None
    buyers_m5: int | None = None
    price_change_m5_pct: float | None = None
    source: str = "imported"

    def validate(self) -> None:
        if not self.pool or not self.token or self.observed_at <= 0:
            raise ValueError("missing pool/token or invalid observed_at")
        if not isfinite(self.price_usd) or self.price_usd <= 0:
            raise ValueError("price_usd must be positive and finite")
        if not isfinite(self.liquidity_usd) or self.liquidity_usd < 0:
            raise ValueError("liquidity_usd must be finite and >= 0")
        if self.created_at is not None and self.created_at > self.observed_at + 300:
            raise ValueError("pool created_at cannot be in the future")
        for field in ("buys_m5", "sells_m5", "buyers_m5"):
            val = getattr(self, field)
            if val is not None and val < 0:
                raise ValueError(f"{field} must be >= 0")
        for field in ("volume_m5_usd", "price_change_m5_pct"):
            val = getattr(self, field)
            if val is not None and (not isfinite(val) or (field == "volume_m5_usd" and val < 0)):
                raise ValueError(f"invalid {field}")


@dataclass(frozen=True)
class SecurityObservation:
    token: str
    observed_at: int
    mint_revoked: bool | None
    freeze_revoked: bool | None
    top10_pct: float | None
    source: str = "user_import"
    holder_updated_at: int | None = None

    def validate(self) -> None:
        if not self.token or self.observed_at <= 0:
            raise ValueError("invalid security observation")
        if self.holder_updated_at is not None and not 0 < self.holder_updated_at <= self.observed_at:
            raise ValueError("holder timestamp must not be in the future")
        if self.top10_pct is not None and (not isfinite(self.top10_pct) or not 0 <= self.top10_pct <= 100):
            raise ValueError("top10_pct must be 0..100")
        if not self.source:
            raise ValueError("missing security source")
