"""Synthetic fixture only. Not real transactions or an empirical backtest."""
from __future__ import annotations
from datetime import datetime, timezone
from .models import Snapshot, SecurityObservation


def make_demo() -> tuple[list[Snapshot], list[SecurityObservation]]:
    start = int(datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc).timestamp())
    prices = {
        "ALPHA": [1,1.02,1.04,1.06,1.11,1.16,1.2,1.25,1.29,1.32,1.36,1.41,1.38,1.31,1.24,1.18,1.15,1.16,1.15,1.12,1.1,1.08,1.06,1.02,1.01,1],
        "BETA": [1,1.01,1.02,1.02,1.0,.98,.95,.91,.88,.86,.85,.83,.81,.82,.79,.77,.75,.74,.72,.70,.69,.68,.68,.67,.65,.64],
        "GAMMA": [1,1,1.001,1.002,1.003,1.004,1.005,1.006,1.007,1.008,1.009,1.01,1.011,1.012,1.012,1.013,1.014,1.015,1.016,1.017,1.017,1.018,1.019,1.02,1.021,1.022],
        "LOWLIQ": [1+i*.05 for i in range(26)],
        "CONCENTRATED": [1+i*.03 for i in range(26)],
        "UNKNOWN": [1+i*.1 for i in range(26)],
    }
    volumes = {"ALPHA": 8000, "BETA": 5000, "GAMMA": 500, "LOWLIQ": 10000,
               "CONCENTRATED": 10000, "UNKNOWN": 10000}
    liquids = {"ALPHA": 60000, "BETA": 48000, "GAMMA": 100000, "LOWLIQ": 12000,
               "CONCENTRATED": 90000, "UNKNOWN": 70000}
    snapshots: list[Snapshot] = []
    for minute in range(26):
        for token, series in prices.items():
            bullish = token != "GAMMA" and (token != "BETA" or minute <= 3)
            snapshots.append(Snapshot(
                pool=f"pool_{token}", token=f"synthetic_{token}", observed_at=start+60*minute,
                price_usd=round(0.0001*series[minute], 12),
                liquidity_usd=liquids[token], pool_name=f"{token} / SOL",
                created_at=start-120, volume_m5_usd=volumes[token],
                buys_m5=25 if bullish else 3,
                sells_m5=4 if bullish else 18,
                buyers_m5=23 if bullish else 3,
                price_change_m5_pct=(5 if bullish else -2),
                source="synthetic"
            ))
    securities = [SecurityObservation(token=f"synthetic_{token}", observed_at=start-180,
              mint_revoked=True, freeze_revoked=True,
              top10_pct=45.0 if token == "CONCENTRATED" else 12.0,
              source="synthetic")
              for token in ("ALPHA","BETA","GAMMA","LOWLIQ","CONCENTRATED")]
    return snapshots, securities
