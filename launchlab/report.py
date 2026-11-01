"""Self-contained, mobile-friendly offline HTML research report."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from math import isfinite
from typing import Any
from .engine import Config, SecurityIndex, security_reason
from .models import SecurityObservation, Snapshot


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _f(n: float | None, digits: int = 2) -> str:
    return "–" if n is None else f"{n:,.{digits}f}"


def _percent(n: float) -> str:
    return ("+" if n > 0 else "") + f"{n:.2f}%"


def _graph(strategies: dict[str, dict[str, Any]], initial_cash: float) -> str:
    w, h = 850, 260
    pad_l, pad_r, pad_t, pad_b = 45, 12, 16, 36
    curves = [s["curve"] for s in strategies.values() if s["curve"]]
    if not curves:
        return "<p>Not enough observations to chart performance.</p>"
    times = [p["time"] for curve in curves for p in curve]
    vals = [p["equity"] for curve in curves for p in curve]
    lo, hi = min(vals), max(vals)
    lo = min(lo, initial_cash)
    hi = max(hi, initial_cash)
    margin = max(1.0, (hi - lo)*.1)
    lo, hi = lo-margin, hi+margin
    tmin, tmax = min(times), max(times)
    def x(t: float) -> float:
        return pad_l + (t-tmin)/max(1,tmax-tmin)*(w-pad_l-pad_r)
    def y(v: float) -> float:
        return pad_t + (hi-v)/(hi-lo)*(h-pad_t-pad_b)
    shapes = []
    for i in range(5):
        v = lo + (hi-lo)*i/4
        yy = y(v)
        shapes.append(f'<line x1="{pad_l}" x2="{w-pad_r}" y1="{yy:.1f}" y2="{yy:.1f}" stroke="#263148" stroke-dasharray="3 5"/>')
        shapes.append(f'<text x="{pad_l-8}" y="{yy+4:.1f}" text-anchor="end" fill="#8794ae" font-size="11">${v:.0f}</text>')
    colors = {"momentum": "#74e3c4", "random_at_signal_times": "#a9b9ff", "safety_only": "#ffbe83"}
    for name, obj in strategies.items():
        pts = obj["curve"]
        if not pts:
            continue
        d = " ".join(f"{x(p['time']):.1f},{y(p['equity']):.1f}" for p in pts)
        shapes.append(f'<polyline points="{d}" fill="none" stroke="{colors.get(name,"#fff")}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>')
        last = pts[-1]
        shapes.append(f'<circle cx="{x(last["time"]):.1f}" cy="{y(last["equity"]):.1f}" r="4" fill="{colors.get(name,"#fff")}"/>')
    shapes.append(f'<text x="{pad_l}" y="{h-8}" fill="#8794ae" font-size="12">{escape(_date(tmin))}</text>')
    shapes.append(f'<text x="{w-pad_r}" y="{h-8}" text-anchor="end" fill="#8794ae" font-size="12">{escape(_date(tmax))}</text>')
    return f'<svg class="chart" role="img" aria-label="Simulated equity by strategy over time" viewBox="0 0 {w} {h}" preserveAspectRatio="xMidYMid meet">{"".join(shapes)}</svg>'


def render_report(result: dict[str, Any], snapshots: list[Snapshot], securities: list[SecurityObservation]) -> str:
    mode = result["data_mode"]
    simulated = mode == "synthetic"
    cfg = Config(**result["config"])
    index = SecurityIndex(securities)
    latest: dict[str, Snapshot] = {}
    for s in sorted(snapshots, key=lambda p: p.observed_at):
        latest[s.pool] = s
    observed_at = result["period"]["last_observed_at"]
    filtered: list[tuple[Snapshot,str]] = []
    for s in sorted(latest.values(), key=lambda p: (-p.liquidity_usd, p.pool)):
        sec = index.at(s.token, observed_at)
        reason = security_reason(s, sec, cfg, now=observed_at)
        status = "eligible" if reason is None else reason
        filtered.append((s, status))
    strategies = result["strategies"]
    title = "Synthetic demo" if simulated else "Research dataset"
    arm_labels = {"momentum": "Momentum rules", "random_at_signal_times": "Random at signal times", "safety_only": "Safety-only selection"}
    cards = ""
    for name in ("momentum", "random_at_signal_times", "safety_only"):
        s = strategies[name]
        c = "up" if s["return_pct"] > 0 else "down" if s["return_pct"] < 0 else "flat"
        cards += f'''<div class="strategy card">
           <div class="subtle">{escape(arm_labels[name])}</div>
           <div class="return {c}">{_percent(s["return_pct"])}</div>
           <div class="equity">${_f(s["equity_usd"])} <span>marked equity</span></div>
           <div class="split"><div><b>{s["closed_trades"]}</b><small>closed trades</small></div>
             <div><b>{_f(s["max_drawdown_pct"])}%</b><small>max drawdown</small></div>
             <div><b>{s["open_positions"]}</b><small>open positions</small></div></div>
           <div class="subtle foot">Closed P&L ${_f(s["closed_pnl_usd"])} · Fees ${_f(s["platform_network_fees_usd"])} · Pending {s["pending_orders"]}</div>
        </div>'''
    rows = ""
    for s, status in filtered[:100]:
        label = s.pool_name or s.token[:18]
        attr = escape(label, quote=True).lower()
        badge = "positive" if status == "eligible" else "negative" if status in ("low_liquidity","active_authority","concentrated_holders") else "warning"
        rows += f'''<tr data-search="{attr} {escape(s.token.lower(),quote=True)} {escape(s.pool.lower(),quote=True)}">
          <td><strong>{escape(label)}</strong><small>{escape(s.pool[:9])}…</small></td>
          <td class="mono">${_f(s.liquidity_usd,0)}</td>
          <td class="mono">${_f(s.volume_m5_usd,0)}</td>
          <td class="mono">{s.buys_m5 if s.buys_m5 is not None else '–'} / {s.sells_m5 if s.sells_m5 is not None else '–'}</td>
          <td><span class="status {badge}">{escape(status.replace('_',' '))}</span></td>
          <td class="mono">{escape(_date(s.observed_at))}</td>
        </tr>'''
    trade_rows = ""
    for trade in strategies["momentum"]["trades"]:
        pnl = f'${_f(trade.get("pnl_usd"))}' if trade["kind"] == "sell" else "–"
        trade_rows += f'''<tr><td>{escape(_date(trade['time']))}</td><td>{escape(trade['kind'])}</td>
          <td>{escape(trade['token'])}</td><td class="mono">${_f(trade['amount_usd'])}</td>
          <td class="mono">{pnl}</td><td>{escape(trade['reason'].replace('_',' '))}</td></tr>'''
    if not trade_rows:
        trade_rows = '<tr><td colspan="6" class="empty">No filled trades. The security gate may require observations, or there may not be enough consecutive price samples.</td></tr>'
    reasons = result["coverage"]["rejections_on_new_quotes"]
    explain = ", ".join(f"{k.replace('_',' ')}: {v}" for k, v in sorted(reasons.items())) or "No exclusions recorded"
    banner = "All prices, volumes and P&L below are fabricated test data. No market-performance conclusion is possible." if simulated else "Research estimates from supplied/collected observations, not verified wallet P&L. Live API execution has not been validated in this environment."
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
    <title>LaunchLab · {title}</title><style>
    :root {{color-scheme:dark; font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:#0b1020;color:#eaf1ff}}
    *{{box-sizing:border-box}} body{{margin:0}} .shell{{max-width:1250px;margin:0 auto;padding:32px 22px 80px}}
    .top{{display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #263148;padding:0 0 24px;gap:15px}}
    .brand{{font-size:21px;font-weight:750;letter-spacing:-.7px}} .brand span{{color:#74e3c4}}
    .eyebrow{{color:#94a3b8;letter-spacing:1.3px;text-transform:uppercase;font-size:11px;font-weight:750}}
    h1{{font-size:clamp(30px,4.3vw,52px);line-height:1.1;letter-spacing:-2px;margin:20px 0 12px;font-weight:720}}
    h2{{font-size:19px;letter-spacing:-.35px;margin:0 0 18px}}p{{line-height:1.65;color:#bdc7d9}}
    .subtle{{font-size:13px;color:#95a3ba}} .pill{{font-size:12px;border-radius:20px;border:1px solid #42546b;padding:7px 11px;color:#c9d8f2;white-space:nowrap}}
    .notice{{border:1px solid #7b5b29;background:#271f19;color:#ffd3a4;border-radius:12px;padding:13px 17px;margin:24px 0 8px;font-size:13px;line-height:1.5}}
    .intro{{max-width:780px}} .facts{{display:flex;gap:20px;flex-wrap:wrap;margin:24px 0 38px;color:#aebcd0;font-size:13px}}
    .facts b{{color:#eaf1ff;font-variant-numeric:tabular-nums}} .cards{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:15px}}
    .card,.panel{{background:#121b2c;border:1px solid #263148;border-radius:16px;padding:23px;min-width:0}}
    .return{{font-size:clamp(28px,4vw,44px);font-weight:730;letter-spacing:-1.8px;margin:12px 0 1px;font-variant-numeric:tabular-nums}}
    .up{{color:#74e3c4}} .down{{color:#ff9f9b}} .flat{{color:#d7dffa}} .equity{{font-size:16px;font-variant-numeric:tabular-nums}} .equity span{{font-size:12px;color:#95a3ba}}
    .split{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;border-top:1px solid #27324a;margin:23px 0 16px;padding-top:17px}}
    .split b{{display:block;font-size:15px;font-variant-numeric:tabular-nums}} .split small{{display:block;font-size:11px;color:#95a3ba;margin-top:4px}}
    .foot{{line-height:1.5}}.section{{margin-top:28px}} .panel{{padding:25px}}
    .chart{{display:block;width:100%;height:auto;min-height:200px}} .legend{{display:flex;gap:20px;flex-wrap:wrap;margin:0 0 20px;color:#bac7d9;font-size:12px}}
    .dot{{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}}
    .bar{{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:16px}}
    input{{border-radius:8px;border:1px solid #36445c;background:#0e1523;color:#f7faff;padding:11px 13px;width:240px;max-width:100%}}
    .overflow{{overflow:auto}}table{{width:100%;border-collapse:collapse;text-align:left;white-space:nowrap;font-size:13px}}
    th{{color:#95a3ba;font-size:11px;text-transform:uppercase;letter-spacing:.55px;font-weight:650;padding:13px 14px;border-bottom:1px solid #303e54}}
    td{{padding:15px 14px;border-bottom:1px solid #263148;color:#cad4e5}}td strong{{color:#f0f6ff}}td small{{display:block;font-size:11px;color:#8294b0;margin-top:4px}}
    .mono{{font-variant-numeric:tabular-nums}}.status{{display:inline-block;border-radius:5px;padding:5px 8px;font-size:11px;text-transform:capitalize}}
    .status.positive{{background:#133b33;color:#a7ead6}} .status.negative{{background:#3a2228;color:#ffc2c1}}.status.warning{{background:#3e341f;color:#ffe2a6}}
    .empty{{padding:40px 12px;white-space:normal;text-align:center;color:#94a3b8}}
    .method{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;margin:0;padding:0 10px}} .method li{{margin-bottom:12px;color:#becbe0;line-height:1.5}}
    footer{{margin-top:30px;font-size:12px;color:#7c8da8}}
    @media(max-width:830px){{.cards{{grid-template-columns:1fr}}.method{{grid-template-columns:1fr}}.shell{{padding:22px 15px 70px}}.card,.panel{{padding:18px}}h1{{letter-spacing:-1.5px}}}}
    </style></head><body><main class="shell">
    <header class="top"><div class="brand">Launch<span>Lab</span> <span style="font-weight:400;color:#637594">/ research</span></div><div class="pill">READ ONLY · NO WALLET</div></header>
    <section><div class="eyebrow" style="margin-top:34px">Token signal experiment / v0.2</div>
      <h1>Measure the edge.<br/>Ignore the screenshots.</h1>
      <p class="intro">Time-stamped market observations, explicit safety gates and comparable paper strategies. No LLM-generated trades, no fabricated live performance.</p>
      <div class="notice"><strong>{escape(title)}:</strong> {escape(banner)}</div>
      <div class="facts"><div><b>{result['coverage']['snapshots']}</b> observations</div>
        <div><b>{result['coverage']['pools']}</b> pools</div>
        <div><b>{result['coverage']['minutes_with_momentum_candidates']}</b> signal minutes</div>
        <div>Last observed <b>{escape(_date(observed_at))}</b></div></div>
    </section>
    <div class="cards">{cards}</div>
    <section class="panel section"><h2>Simulated account equity</h2>
      <div class="legend"><span><i class="dot" style="background:#74e3c4"></i>Momentum</span>
      <span><i class="dot" style="background:#a9b9ff"></i>Random, same signal times</span>
      <span><i class="dot" style="background:#ffbe83"></i>Safety-only</span></div>
      {_graph(strategies, cfg.initial_cash)}<p class="subtle">Missing or stale position quotes are marked at $0. Values include modeled fees and price impact, not verified fills.</p></section>
    <section class="panel section"><div class="bar"><div><h2 style="margin-bottom:6px">Observed pools</h2>
        <div class="subtle">Latest observation per pool. Status uses security observations no later than the evaluation timestamp.</div></div>
        <input id="filter" placeholder="Filter name, mint or pool" aria-label="Filter observed pools" /></div>
      <div class="overflow"><table id="pools"><thead><tr><th>Pool</th><th>Liquidity</th><th>5m volume</th><th>5m buys / sells</th><th>Risk status</th><th>Observed</th></tr></thead>
        <tbody>{rows if rows else '<tr><td colspan="6" class="empty">No pools collected.</td></tr>'}</tbody></table></div>
        <p class="subtle">Exclusion counts on incoming observations: {escape(explain)}. “Eligible” means only that these specific configured filters passed.</p></section>
    <section class="panel section"><h2>Momentum strategy's simulated orders</h2>
      <div class="overflow"><table><thead><tr><th>Filled</th><th>Side</th><th>Token</th><th>Net cash</th><th>Closed P&L</th><th>Reason</th></tr></thead><tbody>{trade_rows}</tbody></table></div></section>
    <section class="panel section"><h2>Research method and limitations</h2><ul class="method">
       <li><strong>Safety first.</strong> Require contemporaneous evidence that mint/freeze authorities are revoked, top 10 ownership ≤{cfg.max_top10_pct:.0f}%, and pool liquidity ≥${cfg.min_liquidity_usd:,.0f}. Unknown security fails closed.</li>
       <li><strong>Signal.</strong> Require 5-minute volume ≥${cfg.min_volume_m5_usd:,.0f}, buys ≥{cfg.min_buy_transactions_m5}, buy fraction ≥{cfg.min_buy_fraction_m5:.0%}, and 5-minute price change {cfg.min_price_change_m5_pct:.0f}–{cfg.max_price_change_m5_pct:.0f}%.</li>
       <li><strong>No same-tick fills.</strong> Each decision uses only data recorded by that minute and may fill at the next observed price for the same pool, never the signal observation.</li>
       <li><strong>Fill-time checks.</strong> Queued entries are discarded if mint/freeze/holder evidence is unsafe or stale, the pool changes tokens, liquidity falls below the threshold, or the quote moves more than {cfg.max_entry_price_deviation_fraction:.0%} from the signal price.</li>
       <li><strong>Execution proxy.</strong> Position size {cfg.position_fraction:.0%}, fixed ${cfg.platform_min_fee_usd:.2f} minimum platform fee per side, constant-product price impact, pool fees and an additional slippage buffer.</li>
       <li><strong>Exit behavior.</strong> 6% stop and 12% trailing drawdown trigger an order that can fill only at a subsequent observation; unfilled stops may remain exposed.</li>
       <li><strong>Not a live execution test.</strong> No transaction-level order book, priority-tip bidding, concentrated liquidity routing, MEV adversaries, or reliable estimate of unsellable tokens.</li>
       <li><strong>Not proof of predictive power.</strong> This is a tool for future forward-collected data and matched comparisons. Backtests need out-of-sample evaluation and uncertainty estimates.</li>
    </ul></section>
    <footer>LaunchLab 0.2 · local static HTML · No external scripts, trackers, connections, or signing keys. Data and assumptions are inspectable in the accompanying JSON and source code.</footer>
    </main><script>document.querySelector('#filter').addEventListener('input',function(){{let q=this.value.toLowerCase();document.querySelectorAll('#pools tbody tr').forEach(r=>{{r.hidden=!(r.dataset.search||'').includes(q)}})}});</script>
    </body></html>'''
