"""No-network tests for conservative direct-on-chain security observation."""
import json
import unittest
from unittest.mock import patch, MagicMock
from launchlab.onchain import (
    CLASSIC_TOKEN_PROGRAM, parse_mint, parse_concentration,
    fetch_onchain_security, rpc_call,
)

MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
ACCOUNTS = ["A" * 32, "B" * 32, "C" * 32, "D" * 32,
            "E" * 32, "F" * 32, "G" * 32, "H" * 32,
            "J" * 32, "K" * 32, "L" * 32]


def mint_payload(*, owner=CLASSIC_TOKEN_PROGRAM, info=None, slot=1000):
    if info is None:
        info = {"mintAuthority": None, "freezeAuthority": None, "supply": "1000"}
    return {"result": {"context": {"slot": slot}, "value": {"owner": owner,
            "data": {"parsed": {"type": "mint", "info": info}}}}}


def holders_payload(*, slot=1001, amounts=None):
    if amounts is None:
        amounts = ["10"] * 11
    return {"result": {"context": {"slot": slot},
            "value": [{"address": a, "amount": n} for a, n in zip(ACCOUNTS, amounts)]}}


class MintTests(unittest.TestCase):
    def test_revoked_classic_mint(self):
        self.assertEqual(parse_mint(mint_payload()), (True, True, 1000, 1000))

    def test_active_authorities(self):
        flags = {"mintAuthority": MINT, "freezeAuthority": MINT, "supply": "1234"}
        self.assertEqual(parse_mint(mint_payload(info=flags))[:3], (False, False, 1234))

    def test_missing_authority_is_unknown(self):
        self.assertEqual(parse_mint(mint_payload(info={"supply": "1000"}))[:3],
                         (None, None, 1000))

    def test_token_2022_unknown_until_extensions_analyzed(self):
        self.assertEqual(parse_mint(mint_payload(owner="TokenzQdBNbLqP5VEhdkAS6EPFgtC2vdMNEfDXWTb"))[:3],
                         (None, None, None))

    def test_account_missing_rejected(self):
        with self.assertRaises(ValueError):
            parse_mint({"result": {"context": {"slot": 1}, "value": None}})


class ConcentrationTests(unittest.TestCase):
    def test_amounts_not_decimals_or_float_ui_amount(self):
        self.assertEqual(parse_concentration(holders_payload(), 1000, mint_slot=1000), 10.0)

    def test_stale_slot_is_unknown(self):
        self.assertIsNone(parse_concentration(holders_payload(slot=3000), 1000, mint_slot=1000))

    def test_zero_supply_not_safe(self):
        self.assertIsNone(parse_concentration(holders_payload(), 0, mint_slot=1000))

    def test_duplicate_account_not_double_counted(self):
        data = holders_payload()
        data["result"]["value"][1]["address"] = ACCOUNTS[0]
        self.assertIsNone(parse_concentration(data, 1000, mint_slot=1000))

    def test_sum_exceeding_supply_is_unknown(self):
        self.assertIsNone(parse_concentration(holders_payload(amounts=["1000"] * 11),
                                               1000, mint_slot=1000))

    def test_large_supply_is_exact(self):
        data = holders_payload(amounts=["922337203685477580"] + ["0"] * 10)
        result = parse_concentration(data, 9223372036854775800, mint_slot=1000)
        self.assertAlmostEqual(result, 10.0)


class RpcSafetyTests(unittest.TestCase):
    def test_only_readonly_methods_supported(self):
        with self.assertRaises(ValueError):
            rpc_call("sendTransaction", MINT)
        with self.assertRaises(ValueError):
            rpc_call("getAccountInfo", "not a pubkey")
        with self.assertRaises(ValueError):
            rpc_call("getAccountInfo", MINT, endpoint="http://localhost:8899")

    def test_readonly_post_request(self):
        response = MagicMock()
        response.read.return_value = b'{"jsonrpc":"2.0","result":{"context":{"slot":1000},"value":null}}'
        response.__enter__.return_value = response
        with patch("launchlab.onchain.urllib.request.urlopen", return_value=response) as urlopen:
            rpc_call("getAccountInfo", MINT)
        sent = urlopen.call_args.args[0]
        data = json.loads(sent.data)
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(data["method"], "getAccountInfo")
        self.assertEqual(data["params"][1]["encoding"], "jsonParsed")

    def test_incomplete_onchain_holder_data_fails_closed(self):
        with patch("launchlab.onchain.rpc_call", side_effect=[mint_payload(),
                 RuntimeError("getTokenLargestAccounts unavailable")]), \
             patch("launchlab.onchain.time.time", return_value=1799999990):
            result = fetch_onchain_security(MINT)
        self.assertTrue(result.mint_revoked)
        self.assertIsNone(result.top10_pct)
        self.assertIsNone(result.holder_updated_at)

    def test_full_observation_is_timestamped_and_explicit(self):
        with patch("launchlab.onchain.rpc_call", side_effect=[mint_payload(), holders_payload()]), \
             patch("launchlab.onchain.time.time", return_value=1799999990):
            result = fetch_onchain_security(MINT)
        self.assertEqual(result.source, "solana_rpc_raw_token_accounts")
        self.assertEqual(result.holder_updated_at, 1799999990)
        self.assertEqual(result.top10_pct, 10.0)


if __name__ == "__main__":
    unittest.main()
