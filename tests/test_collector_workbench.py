"""工作台结果兼容与历史素材隔离；只操作临时数据库。"""
import shutil
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from pintan.collector import collect_snapshot
from pintan.webapp import create_app
from pintan.database import ProductAsset, session_scope

ROOT=Path(__file__).resolve().parents[1]
class CollectorWorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        for name in ['pintan_stable_products.csv','pintan_market_trending_products.csv','pintan_trend_search_results.csv']:
            shutil.copy2(ROOT/name,self.root/name)
        self.client=TestClient(create_app(self.root),base_url='http://127.0.0.1:8010')
    def tearDown(self):self.tmp.cleanup()
    def test_history_not_counted_as_new_images(self):
        original=collect_snapshot(self.root,{'page_url':'https://detail.1688.com/offer/123.html','title':'商品','images':[{'url':'https://cbu01.alicdn.com/fixture.png','region':'主图'}]},False)
        current=collect_snapshot(self.root,{'page_url':original['page_url'],'title':'商品'},False)
        old=self.client.get('/api/collector/runs/'+original['run_id']).json()
        new=self.client.get('/api/collector/runs/'+current['run_id']).json()
        self.assertTrue(old['images'][0]['current_run'])
        self.assertFalse(new['images'][0]['current_run'])
        self.assertEqual(new['image_diagnostics']['found'],0)
        self.assertTrue(new['can_confirm'])
    def test_same_image_classification_evidence_reaches_frontend(self):
        r=collect_snapshot(self.root,{'page_url':'https://detail.1688.com/offer/123.html','title':'商品','images':[{'url':'https://cbu01.alicdn.com/fixture.png','region':kind} for kind in ['主图','SKU图']]},False)
        view=self.client.get('/api/collector/runs/'+r['run_id']).json()
        source_images=[x for x in view['images'] if x['source_url']]
        self.assertEqual(len(source_images),1)
        self.assertEqual(source_images[0]['asset_type'],'SKU图')
        self.assertEqual(len(source_images[0]['capture_metadata']['region_evidence']),2)
        self.assertIsNone(source_images[0]['preview_url'])
        # 即便旧记录没有持久化占位行，工作台仍会给出内容生产所需的三类需求。
        requirements={x['asset_type'] for x in view['images'] if x.get('virtual_requirement')}
        self.assertEqual(requirements,{'主图','功能图','参数图','场景图'})
    def test_url_only_automatic_does_not_fall_back_to_empty_manual_import(self):
        r=self.client.post('/api/collector/tasks',headers={'Origin':'http://127.0.0.1:8010'},json={'page_url':'https://detail.1688.com/offer/123.html'})
        self.assertEqual(r.status_code,409)
        self.assertEqual(self.client.get('/api/collector/runs').json()['items'],[])
        self.assertEqual(self.client.post('/api/collector/capture',json={'page_url':'https://detail.1688.com/offer/123.html','use_model':False}).status_code,422)

    def test_image_classification_and_exclusion_are_drafts_until_confirm(self):
        r=collect_snapshot(self.root,{'page_url':'https://detail.1688.com/offer/123.html','title':'商品',
            'text_blocks':[{'region':'title','text':'商品','source_locator':'fixture.title'},
                           {'region':'attributes','text':'类目：日用品\n材质：PE','source_locator':'fixture.attributes'},
                           {'region':'price','text':'起批量：1件','source_locator':'fixture.price'}],
            'images':[{'url':'https://cbu01.alicdn.com/fixture.png','region':'主图','source_locator':'gallery'}]},False)
        view=self.client.get('/api/collector/runs/'+r['run_id']).json();asset=next(x for x in view['images'] if x['source_url'])
        response=self.client.post(f"/api/collector/runs/{r['run_id']}/assets/{asset['asset_id']}/review",
            json={'asset_type':'SKU图','included':False})
        self.assertEqual(response.status_code,200)
        with session_scope(self.root) as s:
            stored=s.get(ProductAsset,asset['asset_id']);self.assertEqual(stored.asset_type,'主图')
            self.assertNotEqual(stored.status,'复核排除')
        self.client.post('/api/collector/runs/'+r['run_id']+'/confirm')
        with session_scope(self.root) as s:
            stored=s.get(ProductAsset,asset['asset_id']);self.assertEqual(stored.asset_type,'主图')
            self.assertEqual(stored.status,'复核排除')

    def test_non_review_thumbnails_are_auto_passed(self):
        r=collect_snapshot(self.root,{'page_url':'https://detail.1688.com/offer/123.html','title':'商品',
            'images':[{'url':'https://cbu01.alicdn.com/small.png','region':'待分类商品图'},
                      {'url':'https://cbu01.alicdn.com/large.png','region':'待分类商品图'},
                      {'url':'https://cbu01.alicdn.com/sku.png','region':'SKU图'},
                      {'url':'https://cbu01.alicdn.com/review.png','region':'评论图'}]},False)
        with session_scope(self.root) as s:
            for asset in s.query(ProductAsset).all():
                if not asset.source_url:continue
                if asset.source_url.endswith('small.png'):asset.width=150;asset.height=150
                elif asset.source_url.endswith('large.png'):asset.width=1240;asset.height=1920
                else:asset.width=256;asset.height=256
        view=self.client.get('/api/collector/runs/'+r['run_id']).json()
        images={x['source_url'].rsplit('/',1)[-1]:x for x in view['images'] if x['source_url']}
        self.assertTrue(images['small.png']['auto_passed'])
        self.assertFalse(images['large.png']['auto_passed'])
        self.assertTrue(images['sku.png']['auto_passed'])
        self.assertFalse(images['review.png']['auto_passed'])

if __name__=='__main__':unittest.main()
