import tempfile, unittest
from pathlib import Path
from fastapi.testclient import TestClient
from pintan.collector import collect_snapshot, validate_1688_offer_url
from pintan.database import Base, engine

class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);Base.metadata.create_all(engine(self.root))
    def tearDown(self):self.tmp.cleanup()
    def test_url_scope(self):
        self.assertEqual(validate_1688_offer_url('https://detail.1688.com/offer/736208388272.html?x=1'),'736208388272')
        for url in ['https://evil.com/offer/1.html','https://detail.1688.com/search?q=x','file:///tmp/a']:
            with self.assertRaises(ValueError):validate_1688_offer_url(url)
    def test_rules_persist_lineage_and_images(self):
        out=collect_snapshot(self.root,{'page_url':'https://detail.1688.com/offer/736208388272.html','title':'桌边自粘垃圾袋 - 阿里巴巴','visible_text':'￥5.80 10包起批\n材质 PE','image_urls':['https://cbu01.alicdn.com/a.jpg','javascript:bad']},use_model=False)
        self.assertEqual(out['product_id'],'1688-736208388272');self.assertEqual(out['model'],'rules')
        self.assertTrue(any(x['field_name']=='title' and x['source_locator']=='page.title' for x in out['facts']))
        self.assertEqual(len(out['images']),1);self.assertEqual(out['images'][0]['status'],'待下载')
    def test_missing_evidence_stays_empty(self):
        out=collect_snapshot(self.root,{'page_url':'https://detail.1688.com/offer/1.html','title':'普通收纳袋','visible_text':''},use_model=False)
        names={x['field_name'] for x in out['facts']};self.assertEqual(names,{'title'})

if __name__=='__main__':unittest.main()
