"""Read-only GeckoTerminal public API integration.

The provider's public API is cached and rate-limited. We timestamp at receipt
rather than pretending the public endpoint supplies transaction-level history.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any
from .models import Snapshot

API_ROOT = "https://api.geckoterminal.com/api/v2"
HEADERS = {"Accept": "application/json;version=20230203", "User-Agent": "LaunchLab-Research/0.2"}


def get_json(path: str, *, params: dict[str, str | int] | None = None,
             retries: int = 3) -> dict[str, Any]:
    if not path.startswith("/networks/solana/"):
        raise ValueError("Only documented read-only Solana network endpoints permitted")
    url = API_ROOT + path + ("?" + urllib.parse.urlencode(params) if params else "")
    request = urllib.request.Request(url, headers=HEADERS, method="GET")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.load(response)
                if not isinstance(data, dict) or not isinstance(data.get("data"), (list, dict)):
                    raise ValueError("Unexpected GeckoTerminal response structure")
                return data
        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise RuntimeError(f"GeckoTerminal returned HTTP {error.code}") from error
            retry_header = error.headers.get("Retry-After")
            retry = float(retry_header) if retry_header and retry_header.isdigit() else 2 ** (attempt + 1)
            time.sleep(min(60, max(1, retry)))
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == retries - 1:
                raise RuntimeError(f"GeckoTerminal unavailable: {error}") from error
            time.sleep(2 ** (attempt + 1))
    raise AssertionError("unreachable")


def _number(value: object) -> float | None:
    try:
        if value is None or value == "":
            return None
        n = float(value)
        from math import isfinite
        return n if isfinite(n) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None  # Don't reinterpret an ambiguous provider timestamp as local time.
        return int(parsed.timestamp())
    except (ValueError, OverflowError):
        return None


def _token_address(rel: object, included: dict[str, str]) -> str:
    if not isinstance(rel, dict):
        return ""
    info = rel.get("data") or {}
    if not isinstance(info, dict):
        return ""
    key = info.get("id", "")
    if key in included:
        return included[key]
    # GeckoTerminal JSON:API token relationships use 'solana_<mint>'.
    if isinstance(key, str) and key.startswith("solana_"):
        return key[len("solana_"):]
    return ""


def parse_pools(payload: dict[str, Any], observed_at: int | None = None) -> list[Snapshot]:
    observed = observed_at if observed_at is not None else int(time.time())
    included: dict[str, str] = {}
    for obj in payload.get("included", []):
        if not isinstance(obj, dict) or obj.get("type") != "token":
            continue
        address = (obj.get("attributes") or {}).get("address")
        if isinstance(address, str) and address:
            included[obj.get("id", "")] = address
    raw_data = payload.get("data", [])
    records = raw_data if isinstance(raw_data, list) else [raw_data]
    result: list[Snapshot] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        a = item.get("attributes") or {}
        relationships = item.get("relationships") or {}
        pool = a.get("address")
        token = _token_address(relationships.get("base_token"), included)
        price = _number(a.get("base_token_price_usd"))
        liquidity = _number(a.get("reserve_in_usd"))
        if not pool or not token or price is None or price <= 0 or liquidity is None or liquidity < 0:
            continue
        tx = (a.get("transactions") or {}).get("m5") or {}
        volume = (a.get("volume_usd") or {}).get("m5")
        pct = (a.get("price_change_percentage") or {}).get("m5")
        s = Snapshot(pool=str(pool), token=token, observed_at=observed,
                     price_usd=price, liquidity_usd=liquidity,
                     pool_name=str(a.get("name") or ""),
                     created_at=_timestamp(a.get("pool_created_at")),
                     volume_m5_usd=_number(volume),
                     buys_m5=int(tx["buys"]) if _number(tx.get("buys")) is not None else None,
                     sells_m5=int(tx["sells"]) if _number(tx.get("sells")) is not None else None,
                     buyers_m5=int(tx["buyers"]) if _number(tx.get("buyers")) is not None else None,
                     price_change_m5_pct=_number(pct), source="geckoterminal")
        try:
            s.validate()
        except ValueError:
            continue
        result.append(s)
    return result


def fetch_new_pools(page: int = 1) -> list[Snapshot]:
    payload = get_json("/networks/solana/new_pools", params={"page": page, "include": "base_token,quote_token,dex"})
    return parse_pools(payload, observed_at=int(time.time()))


def fetch_pools_by_address(pools: list[str]) -> list[Snapshot]:
    if not pools:
        return []
    # No unvalidated URLs and no arbitrary hosts. Each address is a simple Solana base58 string.
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    if any(not 20 <= len(p) <= 64 or any(c not in alphabet for c in p) for p in pools):
        raise ValueError("Invalid Solana pool address")
    if len(pools) > 20:
        raise ValueError("Max 20 pools per request")
    path = "/networks/solana/pools/multi/" + ",".join(pools)
    return parse_pools(get_json(path, params={"include": "base_token,quote_token,dex"}), observed_at=int(time.time()))


def parse_security_info(payload: dict[str, Any], *, token: str,
                        observed_at: int | None = None,
                        max_holder_age_seconds: int = 1800):
    """Conservatively normalize GeckoTerminal's Solana token-info response.

    A null mint/freeze authority means revoked only if that field is PRESENT.
    Missing/malformed/stale holder information remains unknown and fails the
    safety gate. The returned number describes token-account concentration in
    the data provider, not linked-beneficial-owner concentration.
    """
    from .models import SecurityObservation
    observed = observed_at if observed_at is not None else int(time.time())
    data = payload.get("data") or {}
    if not isinstance(data, dict) or not isinstance(data.get("attributes"), dict):
        raise ValueError("Unrecognized GeckoTerminal token-info response")
    attrs = data["attributes"]
    if attrs.get("address") != token:
        raise ValueError("GeckoTerminal token-info mint does not match request")
    mint = (attrs.get("mint_authority") is None) if "mint_authority" in attrs else None
    freeze = (attrs.get("freeze_authority") is None) if "freeze_authority" in attrs else None
    holders = attrs.get("holders") or {}
    top10 = None
    holder_updated_at = None
    if isinstance(holders, dict):
        distribution = holders.get("distribution_percentage") or {}
        updated = _timestamp(holders.get("last_updated"))
        if (isinstance(distribution, dict) and updated is not None
            and 0 <= observed - updated <= max_holder_age_seconds):
            top10 = _number(distribution.get("top_10"))
            if top10 is not None and 0 <= top10 <= 100:
                holder_updated_at = updated
            else:
                top10 = None
    return SecurityObservation(token=token, observed_at=observed,
          mint_revoked=mint, freeze_revoked=freeze, top10_pct=top10,
          source="geckoterminal_token_info", holder_updated_at=holder_updated_at)


def fetch_token_security(token: str, max_holder_age_seconds: int = 1800):
    alphabet = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    if not 20 <= len(token) <= 64 or any(c not in alphabet for c in token):
        raise ValueError("Invalid Solana mint address")
    payload = get_json(f"/networks/solana/tokens/{token}/info")
    return parse_security_info(payload, token=token,
            observed_at=int(time.time()), max_holder_age_seconds=max_holder_age_seconds)
