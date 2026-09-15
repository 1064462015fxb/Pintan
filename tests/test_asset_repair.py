import tempfile
import io
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from sqlalchemy import select
from pintan.database import Base,engine,session_scope,Product,ProductAsset
from pintan.collector import collect_snapshot
from pintan.collector_images import download_asset
from pintan.asset_records import reconcile,metadata

class AssetRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        Base.metadata.create_all(engine(self.root))
        self.payload={'page_url':'https://detail.1688.com/offer/1.html','title':'测试商品','images':[{'url':'https://cbu01.alicdn.com/a.jpg','region':'SKU图','sku':'白色','source_locator':'skuSelection'}]}
    def tearDown(self):self.tmp.cleanup()
    def test_metadata_and_repeated_capture(self):
        collect_snapshot(self.root,self.payload,False);collect_snapshot(self.root,self.payload,False)
        with session_scope(self.root) as s:
            assets=list(s.scalars(select(ProductAsset)))
            self.assertEqual(len(assets),1);self.assertEqual(assets[0].asset_type,'SKU图')
            self.assertEqual(metadata(s,assets[0].asset_id)['sku'],'白色')
    def test_url_only_rejected(self):
        with self.assertRaisesRegex(ValueError,'仅有链接'):
            collect_snapshot(self.root,{'page_url':self.payload['page_url']},False)
    def test_legacy_url_does_not_invent_main_image(self):
        collect_snapshot(self.root,dict(self.payload,images=[],image_urls=['https://cbu01.alicdn.com/a.jpg']),False)
        with session_scope(self.root) as s:self.assertEqual(s.scalar(select(ProductAsset)).asset_type,'待分类商品图')
    def test_merge_preserves_file_and_main_label(self):
        collect_snapshot(self.root,self.payload,False)
        with session_scope(self.root) as s:
            old=s.scalar(select(ProductAsset));old.asset_type='主图'
            s.add(ProductAsset(asset_id='downloaded',product_id='1688-1',source_url=old.source_url,asset_type='原图',status='待授权确认',local_path='existing.png',width=800,height=800,sha256='hash'))
        with session_scope(self.root) as s:self.assertEqual(reconcile(s,'1688-1'),1)
        with session_scope(self.root) as s:
            self.assertEqual(reconcile(s,'1688-1'),0)
            self.assertEqual(s.get(ProductAsset,'downloaded').asset_type,'主图')
    def test_failed_upgrade_retains_success_and_authorization(self):
        collect_snapshot(self.root,self.payload,False)
        with session_scope(self.root) as s:
            a=s.scalar(select(ProductAsset));aid=a.asset_id;a.local_path='existing.png';a.width=800;a.height=800;a.status='可使用';a.authorization_status='已确认'
        with patch('pintan.collector_images._safe_public_url',side_effect=ValueError('blocked')):result=download_asset(self.root,aid)
        self.assertEqual(result['status'],'可使用')
        with session_scope(self.root) as s:
            self.assertEqual(s.get(ProductAsset,aid).authorization_status,'已确认')
            self.assertTrue(metadata(s,aid)['download_error'])

    def test_same_url_regions_preserved_and_run_id_idempotent(self):
        payload=dict(self.payload,images=[dict(self.payload['images'][0],region=r) for r in ['主图','SKU图','详情图']])
        first=collect_snapshot(self.root,payload,False,run_id='CR-restart')
        self.assertEqual(collect_snapshot(self.root,payload,False,run_id='CR-restart')['run_id'],first['run_id'])
        with session_scope(self.root) as s:
            # 同一图片同时具有主图和SKU关系时，SKU证据优先展示。
            asset=s.scalar(select(ProductAsset));self.assertEqual(asset.asset_type,'SKU图')
            self.assertEqual(len(metadata(s,asset.asset_id)['region_evidence']),3)

    def test_download_bytes_hash_dedup_retains_image_and_classification(self):
        data=io.BytesIO();Image.new('RGB',(800,800),'white').save(data,format='PNG')
        class Response:
            headers={'content-type':'image/png'}
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def raise_for_status(self):pass
            def iter_bytes(self):yield data.getvalue()
        payload=dict(self.payload,images=[{'url':'https://cbu01.alicdn.com/a.png','region':'SKU图'}, {'url':'https://cbu01.alicdn.com/b.png','region':'主图'}])
        collect_snapshot(self.root,payload,False)
        with session_scope(self.root) as s:ids=[a.asset_id for a in s.scalars(select(ProductAsset).order_by(ProductAsset.source_url))]
        with patch('pintan.collector_images._safe_public_url',side_effect=lambda u:u),patch('pintan.collector_images.httpx.stream',return_value=Response()):
            for aid in ids:download_asset(self.root,aid)
        with session_scope(self.root) as s:
            live=[a for a in s.scalars(select(ProductAsset)) if not a.status.startswith('已合并:')]
            self.assertEqual(len(live),1);self.assertEqual(live[0].asset_type,'主图')
            self.assertTrue((self.root/live[0].local_path).exists());self.assertEqual(live[0].width,800)
            self.assertEqual(live[0].authorization_status,'未确认')

if __name__=='__main__':unittest.main()
