import shutil,tempfile,unittest
from pathlib import Path
from pintan.content_flow import ANGLES,PLATFORMS,REQUIRED,scan,save_choice
from pintan.database import AngleDiagnosis,init_database,session_scope,utcnow
ROOT=Path(__file__).resolve().parents[1]
class ContentFlowTests(unittest.TestCase):
 def test_material_does_not_prove_performance(self):
  issues=scan({'卖点':'PE材质防漏，不伤内饰'}, {'claims':[{'value':'PE塑料','verification_status':'已人工复核'}]},'淘宝')
  self.assertTrue(any(x['text']=='防漏' for x in issues))
  self.assertTrue(any(x['text']=='不伤内饰' for x in issues))
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name)
  for n in ["pintan_stable_products.csv","pintan_market_trending_products.csv","pintan_trend_search_results.csv"]:shutil.copy2(ROOT/n,self.root/n)
  init_database(self.root)
 def tearDown(self):self.t.cleanup()
 def test_platform_contracts(self):
  self.assertEqual(PLATFORMS,{"抖音","淘宝","京东","Amazon","小红书"})
  self.assertNotIn("TikTok Shop",PLATFORMS);self.assertNotIn("拼多多",PLATFORMS)
  self.assertTrue(all(REQUIRED[p] for p in PLATFORMS))
 def test_scan(self):
  self.assertTrue(scan({"商品标题":"全网第一 100%静音"},{},"淘宝"))
  self.assertTrue(scan({"Product Title":"x"*76},{},"Amazon"))
  self.assertFalse(scan({"商品标题":"便携风扇"},{},"淘宝"))
  self.assertFalse(scan({"详情页模块":["第一张：商品正面图","第二部分：规格"]},{},"淘宝"))
 def test_user_choice_kept_separate(self):
  with session_scope(self.root) as s:s.add(AngleDiagnosis(diagnosis_id="D",product_id="SUP-FAN-001",trend_id=None,cache_key="k",result_json='{"primary":{"type":"使用场景型"}}',model="m",prompt_version="v",created_at=utcnow()))
  result=save_choice(self.root,"D",["性价比型"],"通勤")
  self.assertEqual(result["selected_angles"],["性价比型"]);self.assertEqual(result["custom_angle"],"通勤")
