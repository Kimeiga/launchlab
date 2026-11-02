"""Check live read-only Solana RPC mint parsing; never sends transactions."""
from __future__ import annotations
import json
from launchlab.onchain import fetch_onchain_security

# This address is the public USDC token mint on Solana, not a credential.
PUBLIC_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def main() -> int:
    obs = fetch_onchain_security(PUBLIC_USDC_MINT)
    if obs.mint_revoked is None or obs.freeze_revoked is None:
        raise RuntimeError("Could not verify classic SPL Token mint authorities")
    print(json.dumps({
        "result": "passed", "source": obs.source,
        "mint_authority_observable": obs.mint_revoked is not None,
        "freeze_authority_observable": obs.freeze_revoked is not None,
        "raw_account_concentration_observable": obs.top10_pct is not None,
        "note": "Known public mint, not a recently launched token; no trades made.",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
