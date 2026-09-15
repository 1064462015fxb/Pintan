"""工作台接口只丰富展示字段，不改变评分和业务数据。"""
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from pintan.webapp import create_app, with_product_link


class WorkspaceTests(unittest.TestCase):
    def test_links(self):
        for url in ['', 'javascript:alert(1)', 'https://s.1688.com/selloffer/offer_search.htm', 'https://a.com/search?q=x', 'https://user:pass@a.com/1', 'https://a.com:bad/1']:
            self.assertIsNone(with_product_link({'product_url': url})['detail_url'])
        url = 'https://detail.1688.com/offer/123.html'
        self.assertEqual(with_product_link({'offer_url': url}, 'offer_url')['detail_url'], url)
        self.assertIsNone(with_product_link({'source_query_url':url})['detail_url'])

    def test_read_only_api(self):
        root = Path(__file__).resolve().parents[1]
        client = TestClient(create_app(root))
        data = client.get('/api/stable').json()
        self.assertEqual(len(data['items']),30)
        self.assertEqual(data['top10'],data['items'][:10])
        for trend in client.get('/api/trends').json()['items']:
            data = client.get('/api/trends/'+trend['trend_id']).json()
            self.assertEqual(len(data['all10']),10)
            self.assertTrue(all(x['trend_id']==trend['trend_id'] for x in data['all10']))
            self.assertTrue(all('detail_link_status' in x for x in data['all10']))
