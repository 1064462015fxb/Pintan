from __future__ import annotations
import csv, tempfile, unittest
from pathlib import Path
from shutil import copy2
from pintan.agents import _validate_assessments
from pintan.data_store import read_csv, validate_three_tables
from pintan.demo_data import initialize_demo
from pintan.stable import score_stable_products
ROOT=Path(__file__).resolve().parents[1]

class NewFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        for name in ("pintan_1688_supply_candidates.csv","pintan_market_trending_products.csv"): copy2(ROOT/name,self.root)
        initialize_demo(self.root)
    def tearDown(self): self.temp.cleanup()
    def test_three_tables_and_ten_candidates(self):
        self.assertEqual(validate_three_tables(self.root),{"stable_products":30,"trends":20,"search_results":200})
        with self.assertRaises(FileExistsError): initialize_demo(self.root)
    def test_stable_deterministic_without_trends(self):
        first=score_stable_products(self.root); (self.root/"pintan_market_trending_products.csv").unlink(); second=score_stable_products(self.root)
        self.assertEqual([(x["stable_id"],x["total_score"]) for x in first],[(x["stable_id"],x["total_score"]) for x in second])
    def test_lower_retail_does_not_increase_margin(self):
        rows=score_stable_products(self.root); target=rows[0]; old=float(target["margin_score"]); target["reference_retail_price_cny"]=str(float(target["reference_retail_price_cny"])*.8)
        path=self.root/"pintan_stable_products.csv"
        with path.open("w",encoding="utf-8-sig",newline="") as f: writer=csv.DictWriter(f,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
        changed=next(x for x in score_stable_products(self.root) if x["stable_id"]==target["stable_id"]); self.assertLessEqual(float(changed["margin_score"]),old)
    def test_model_output_requires_ten_and_math(self):
        trend=read_csv(self.root/"pintan_market_trending_products.csv")[0]; candidates=[x for x in read_csv(self.root/"pintan_trend_search_results.csv") if x["trend_id"]==trend["trend_id"]]
        def item(c): return {"search_id":c["search_id"],"feature_match_score":60,"price_advantage_score":70,"pain_resolution_score":50,"supplier_quality_score":80,"match_score":63.5,"recommendation":"谨慎测试","recommendation_reason":"基于需求与候选字段","resolved_pain_points":[],"unresolved_pain_points":trend["negative_review_pain_points"].split("|"),"risk_flags":["待复核"],"evidence_fields":["trend.core_features","search.feature_tags"]}
        self.assertEqual(len(_validate_assessments([item(x) for x in candidates],trend,candidates)),10)
        with self.assertRaises(ValueError): _validate_assessments([item(x) for x in candidates[:9]],trend,candidates)
if __name__=="__main__": unittest.main()
