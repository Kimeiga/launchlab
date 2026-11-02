"""Read-only Solana JSON-RPC observations for classic SPL Token mints.

This supplements GeckoTerminal's often-unavailable fresh token metadata. The
largest-token-accounts API reports accounts, NOT beneficial owners. Pool vaults,
exchanges, and wallets controlled by the same actor distort this metric. It is
not a rug-pull classifier or a security audit.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

from .models import SecurityObservation

PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
CLASSIC_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
BASE58 = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
ALLOWED_METHODS = frozenset(("getAccountInfo", "getTokenLargestAccounts"))


def valid_address(mint: str) -> bool:
    return isinstance(mint, str) and 32 <= len(mint) <= 44 and all(c in BASE58 for c in mint)


def rpc_call(method: str, mint: str, *, endpoint: str | None = None, retries: int = 2) -> dict:
    """Call only two whitelisted read-only methods. Never log endpoint credentials."""
    if method not in ALLOWED_METHODS or not valid_address(mint):
        raise ValueError("Unsupported Solana RPC request")
    url = endpoint or os.getenv("SOLANA_RPC_URL") or PUBLIC_RPC
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError("Solana RPC endpoint must be a valid HTTPS URL")
    opts = {"commitment": "confirmed"}
    if method == "getAccountInfo":
        opts["encoding"] = "jsonParsed"
    req = urllib.request.Request(
        url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                         "params": [mint, opts]}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "LaunchLab-Research/0.3"},
        method="POST",
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise RuntimeError("Malformed Solana RPC response")
            if payload.get("error"):
                raise RuntimeError(f"Solana RPC error ({method}); result unavailable")
            if not isinstance(payload.get("result"), dict):
                raise RuntimeError("Missing Solana RPC result")
            return payload
        except urllib.error.HTTPError as error:
            if attempt == retries - 1 or error.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"Solana RPC HTTP {error.code} ({method})") from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
            if attempt == retries - 1:
                raise RuntimeError(f"Solana RPC transport/JSON error ({method})") from None
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def parse_mint(payload: dict) -> tuple[bool | None, bool | None, int | None, int]:
    """Return (mint_revoked, freeze_revoked, raw_supply, slot).

    Unknown token programs, missing mint fields, and unparsed data fail closed.
    Token-2022 extensions are not yet inspected, so we do not claim safety.
    """
    result = payload.get("result") or {}
    context = result.get("context") or {}
    slot = context.get("slot")
    if not isinstance(slot, int) or slot <= 0:
        raise ValueError("Mint RPC slot is missing")
    account = result.get("value")
    if not isinstance(account, dict):
        raise ValueError("Mint account does not exist")
    if account.get("owner") != CLASSIC_TOKEN_PROGRAM:
        return None, None, None, slot
    data = account.get("data") or {}
    parsed = data.get("parsed") if isinstance(data, dict) else None
    if not isinstance(parsed, dict) or parsed.get("type") != "mint":
        return None, None, None, slot
    info = parsed.get("info") or {}
    if not isinstance(info, dict):
        return None, None, None, slot
    mint = info.get("mintAuthority") is None if "mintAuthority" in info else None
    freeze = info.get("freezeAuthority") is None if "freezeAuthority" in info else None
    try:
        raw_supply = int(info["supply"])
        if raw_supply < 0 or isinstance(info["supply"], bool):
            raise ValueError("Invalid mint supply")
    except (KeyError, TypeError, ValueError):
        raw_supply = None
    return mint, freeze, raw_supply, slot


def parse_concentration(payload: dict, supply: int, *, mint_slot: int,
                        max_slot_skew: int = 150) -> float | None:
    """Top ten raw token *accounts* / mint supply, not owner concentration."""
    if supply <= 0:
        return None
    result = payload.get("result") or {}
    context = result.get("context") or {}
    slot = context.get("slot")
    if not isinstance(slot, int) or abs(slot - mint_slot) > max_slot_skew:
        return None
    accounts = result.get("value")
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 20:
        return None
    balances: list[int] = []
    addresses: set[str] = set()
    for account in accounts:
        if not isinstance(account, dict) or not valid_address(account.get("address", "")):
            return None
        if account["address"] in addresses:
            return None
        addresses.add(account["address"])
        amount = account.get("amount")
        if not isinstance(amount, str) or not amount.isdecimal():
            return None
        balances.append(int(amount))
    balances.sort(reverse=True)
    top10 = sum(balances[:10])
    if top10 > supply:
        return None
    try:
        return float(Decimal(top10) * Decimal(100) / Decimal(supply))
    except (InvalidOperation, OverflowError, ZeroDivisionError):
        return None


def fetch_onchain_security(mint: str) -> SecurityObservation:
    """Fetch observable mint flags and raw account concentration with fresh timestamps."""
    authority = rpc_call("getAccountInfo", mint)
    mint_revoked, freeze_revoked, supply, slot = parse_mint(authority)
    top10 = None
    holder_updated_at = None
    if supply is not None and supply > 0:
        try:
            largest = rpc_call("getTokenLargestAccounts", mint)
            top10 = parse_concentration(largest, supply, mint_slot=slot)
            if top10 is not None:
                holder_updated_at = int(time.time())
        except RuntimeError:
            pass
    observed_at = int(time.time())
    security = SecurityObservation(
        token=mint, observed_at=observed_at,
        mint_revoked=mint_revoked, freeze_revoked=freeze_revoked,
        top10_pct=top10, source="solana_rpc_raw_token_accounts",
        holder_updated_at=holder_updated_at,
    )
    security.validate()
    return security
