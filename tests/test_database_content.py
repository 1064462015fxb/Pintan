import copy,shutil,tempfile,unittest
from pathlib import Path
from pintan.content_master import confirm_version,get_master,save_version,validate_payload
from sqlalchemy import func,select
from pintan.database import Product,SchemaMeta,clear_database_content,db_path,export_csvs,init_database,product_bundle,session_scope
from pintan.data_store import atomic_write_csv_raw

ROOT=Path(__file__).resolve().parents[1]
class DatabaseContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        for name in ["pintan_stable_products.csv","pintan_market_trending_products.csv","pintan_trend_search_results.csv"]: shutil.copy2(ROOT/name,self.root/name)
        init_database(self.root)
    def tearDown(self): self.tmp.cleanup()
    def test_import_idempotence_and_shared_product(self):
        self.assertEqual(init_database(self.root)["stable"],30)
        bundle=product_bundle(self.root,"SUP-FAN-001","TR-DY-001")
        self.assertTrue(bundle["claims"]); self.assertEqual(len(bundle["assets"]),4)
        self.assertTrue(all(x["status"]=="待采集" and x["source_url"] is None and x["ocr_text"] is None for x in bundle["assets"]))
        self.assertTrue(any(x["field_name"]=="demo_description" and x["derivation_rule"] for x in bundle["claims"]))
    def test_export(self):
        out=self.root/"export"; out.mkdir()
        # 导出到根目录的兼容文件已经存在，因此只验证仓库三类数量。
        result=export_csvs(self.root,atomic_write_csv_raw); self.assertEqual(result,{"stable":30,"trend":20,"search":200})
    def test_manual_clear_is_backed_up_and_not_reseeded_on_startup(self):
        backup=clear_database_content(self.root)
        self.assertTrue(backup.exists())
        self.assertEqual(init_database(self.root)["mode"],"manual")
        with session_scope(self.root) as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(Product)),0)
            self.assertEqual(s.get(SchemaMeta,"data_mode").value,"manual")
    def sample(self,bundle):
        cid=bundle["claims"][0]["claim_id"]
        return {"positioning":{"text":"演示","type":"模型创意"},"selling_points":[{"text":"字段支持","claim_ids":[cid],"support_level":"事实支持"}],"pain_points":{"supported":[],"possible":[],"unsupported":[]},"article":{"title_templates":[],"opening":"","sections":[],"call_to_action":""},"video":{"hook":"","shots":[],"call_to_action":""},"image_requirements":[{"asset_type":"主图","brief":"待采集","required_claim_ids":[cid],"status":"待采集"}],"forbidden_expressions":[],"missing_evidence":[],"data_limitations":["待复核"]}
    def test_content_validation(self):
        bundle=product_bundle(self.root,"SUP-FAN-001"); data=self.sample(bundle); validate_payload(data,bundle)
        bad=copy.deepcopy(data); bad["selling_points"][0]["claim_ids"]=["missing"]
        with self.assertRaises(ValueError): validate_payload(bad,bundle)
        bad=copy.deepcopy(data); bad["image_requirements"][0]["status"]="已有原图"
        with self.assertRaises(ValueError): validate_payload(bad,bundle)
