import unittest
from unittest.mock import patch
from launchlab.gecko import parse_pools, fetch_new_pools, get_json

MOCK_DATA = {
  'data': [{
    'id':'solana_poolABC','type':'pool',
    'attributes':{
      'address':'poolABC', 'name':'MINT / SOL',
      'base_token_price_usd':'0.000102', 'reserve_in_usd':'54000.123',
      'pool_created_at':'2026-11-01T12:00:00Z',
      'volume_usd':{'m5':'8000','h24':'50000'},
      'price_change_percentage':{'m5':'7.23'},
      'transactions':{'m5':{'buys':42,'sells':11,'buyers':34,'sellers':9}}
    },
    'relationships':{'base_token':{'data':{'id':'solana_MintToken','type':'token'}}}
  }],
  'included': [{'id':'solana_MintToken','type':'token','attributes':{'address':'MintToken'}}]
}


class GeckoTests(unittest.TestCase):
    def test_parses_official_style_jsonapi_pool(self):
        s = parse_pools(MOCK_DATA, 1793550000)[0]
        self.assertEqual(s.pool, 'poolABC')
        self.assertEqual(s.token, 'MintToken')
        self.assertEqual(s.buys_m5, 42)
        self.assertEqual(s.sells_m5, 11)
        self.assertEqual(s.buyers_m5, 34)
        self.assertEqual(s.price_change_m5_pct, 7.23)
        self.assertEqual(s.liquidity_usd, 54000.123)

    def test_missing_data_not_invented(self):
        data = {'data':[dict(MOCK_DATA['data'][0], attributes={
            'address':'poolABC','base_token_price_usd':'0.1',
            'reserve_in_usd':'50000'})]}
        s = parse_pools(data, 1793550000)[0]
        self.assertIsNone(s.buys_m5)
        self.assertIsNone(s.volume_m5_usd)
        self.assertIsNone(s.price_change_m5_pct)

    def test_rejects_invalid_price(self):
        data = {'data':[dict(MOCK_DATA['data'][0], attributes={
            'address':'poolABC','base_token_price_usd':'NaN',
            'reserve_in_usd':'50000'})]}
        self.assertEqual(parse_pools(data, 1793550000), [])

    def test_get_json_host_whitelist(self):
        with self.assertRaises(ValueError):
            get_json('//evil.com/anything')
        with self.assertRaises(ValueError):
            get_json('/networks/eth/new_pools')

    def test_fetch_new_pool_uses_documented_endpoint(self):
        with patch('launchlab.gecko.get_json', return_value=MOCK_DATA) as mock:
            with patch('launchlab.gecko.time.time', return_value=1793550000):
                res = fetch_new_pools()
            self.assertEqual(len(res), 1)
            self.assertEqual(mock.call_args.args[0], '/networks/solana/new_pools')


if __name__ == '__main__':
    unittest.main()


class SecurityInfoTests(unittest.TestCase):
    def test_complete_current_security_report(self):
        from launchlab.gecko import parse_security_info
        from datetime import datetime, timezone
        now = 1793550000
        date = datetime.fromtimestamp(now-60,timezone.utc).isoformat()
        payload = {'data':{'attributes':{
          'address':'MintToken','mint_authority':None,'freeze_authority':None,
          'holders':{'last_updated':date,'distribution_percentage':{'top_10':'12.9'}}}}}
        s = parse_security_info(payload, token='MintToken', observed_at=now)
        self.assertTrue(s.mint_revoked)
        self.assertTrue(s.freeze_revoked)
        self.assertEqual(s.top10_pct,12.9)

    def test_unknown_and_stale_fields_fail_closed(self):
        from launchlab.gecko import parse_security_info
        from datetime import datetime, timezone
        now = 1793550000
        date = datetime.fromtimestamp(now-3600,timezone.utc).isoformat()
        payload = {'data':{'attributes':{
          'address':'MintToken','holders':{
             'last_updated':date,'distribution_percentage':{'top_10':'12'}}}}}
        s = parse_security_info(payload, token='MintToken', observed_at=now)
        self.assertIsNone(s.mint_revoked)
        self.assertIsNone(s.freeze_revoked)
        self.assertIsNone(s.top10_pct)

    def test_unrevoked_authority_rejected(self):
        from launchlab.gecko import parse_security_info
        payload = {'data':{'attributes':{'address':'MintToken',
            'mint_authority':'someone','freeze_authority':None}}}
        s = parse_security_info(payload, token='MintToken', observed_at=1793550000)
        self.assertFalse(s.mint_revoked)
        self.assertTrue(s.freeze_revoked)
