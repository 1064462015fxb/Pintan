import tempfile,unittest
from pathlib import Path
from sqlalchemy import select
from unittest.mock import Mock,patch

from pintan.collector import collect_snapshot,confirm_run
from pintan.collector_enrichment import collect_ocr_evidence,matched_ocr_fields,run_collection_enrichment,validate_suggestions
from pintan.collector_mapping import detect_review_anomalies,read_mapping,save_review
from pintan.database import Base,ProductAsset,ProductClaim,engine,session_scope


class CollectorEnrichmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        Base.metadata.create_all(engine(self.root))
        self.out=collect_snapshot(self.root,{"page_url":"https://detail.1688.com/offer/123.html",
            "title":"桌边自粘垃圾袋","text_blocks":[
                {"region":"title","text":"桌边自粘垃圾袋","source_locator":"fixture.title"},
                {"region":"attributes","text":"材质：PE","source_locator":"fixture.attributes"},
                {"region":"price","text":"起批量：1包","source_locator":"fixture.price"}]},False)
    def tearDown(self):self.tmp.cleanup()

    def test_validator_only_accepts_missing_inferable_fields_and_real_references(self):
        raw={"suggestions":[{"field_name":"category","value":"垃圾袋","confidence":.7,
            "rationale":"标题包含垃圾袋","based_on_field_ids":["F1"],"risk_note":"AI推测，需人工核验"}]}
        self.assertEqual(validate_suggestions(raw,{"category"},{"F1"})[0]["value"],"垃圾袋")
        bad={"suggestions":[{**raw["suggestions"][0],"field_name":"sales_30d","value":"100"}]}
        with self.assertRaisesRegex(ValueError,"不可验证"):validate_suggestions(bad,{"sales_30d"},{"F1"})
        with self.assertRaisesRegex(ValueError,"不存在"):validate_suggestions(raw,{"category"},{"OTHER"})

    def test_obvious_heading_and_quantity_as_color_are_flagged(self):
        problems=detect_review_anomalies([
            {"review_id":"A","field_name":"title","value":"包装信息","included":True},
            {"review_id":"B","field_name":"color_options","value":"绿色-30只","included":True},
            {"review_id":"C","field_name":"color_options","value":"绿色","included":True},
        ])
        self.assertEqual({x["review_id"] for x in problems},{"A","B"})

    def test_ocr_stops_after_first_image_that_covers_requested_field(self):
        folder=self.root/"data/assets";folder.mkdir(parents=True)
        (folder/"one.png").write_bytes(b"one");(folder/"two.png").write_bytes(b"two")
        with session_scope(self.root) as s:
            for aid,kind,name in [("I1","信息图","one.png"),("I2","详情图","two.png")]:
                s.add(ProductAsset(asset_id=aid,product_id=self.out["product_id"],asset_type=kind,
                    status="待授权确认",local_path="data/assets/"+name,width=800,height=800))
        with patch("pintan.collector_enrichment._ocr",side_effect=["材质：PE","材质：PP"]) as ocr:
            evidence=collect_ocr_evidence(self.root,self.out["product_id"],{"material"})
        self.assertEqual(ocr.call_count,1);self.assertEqual(evidence[0]["matched_fields"],["material"])
        self.assertEqual(matched_ocr_fields("颜色：绿色 规格：30只装",{"product_features"}),{"product_features"})

    def test_ai_suggestion_keeps_inference_lineage_after_human_confirmation(self):
        with session_scope(self.root) as s:
            fields=read_mapping(s,self.out["run_id"])["review_fields"]
            title=fields[0]["review_id"]
            fields.append({"field_name":"category","value":"垃圾袋","included":True,"source_kind":"AI推测",
                "confidence":.72,"rationale":"由标题推测","based_on_field_ids":[title],"risk_note":"需人工核验"})
            save_review(s,self.out["run_id"],fields)
        confirm_run(self.root,self.out["run_id"])
        with session_scope(self.root) as s:
            claim=s.scalar(select(ProductClaim).where(ProductClaim.field_name=="category"))
            self.assertEqual(claim.verification_status,"人工确认的AI推测")
            self.assertEqual(claim.confidence,.72)
            self.assertTrue(claim.source_field.startswith("ai-inference:"))

    def test_invalid_json_is_corrected_once_without_overwriting_captured_fields(self):
        with session_scope(self.root) as s:
            title=read_mapping(s,self.out["run_id"])["review_fields"][0]["review_id"]
        valid={"suggestions":[{"field_name":"category","value":"垃圾袋","confidence":.7,
            "rationale":"标题包含垃圾袋","based_on_field_ids":[title],"risk_note":"AI推测，需人工核验"}]}
        agent=Mock();agent.invoke.return_value={"messages":[]}
        with patch("pintan.collector_enrichment._make_agent",return_value=(agent,"test-model")),\
             patch("pintan.collector_enrichment._skill_trace",return_value={"skills_loaded":["data-enrichment"]}),\
             patch("pintan.collector_enrichment._text",side_effect=["not json",__import__('json').dumps(valid,ensure_ascii=False)]):
            result=run_collection_enrichment(self.root,self.out["run_id"])
        self.assertEqual(agent.invoke.call_count,2);self.assertEqual(result["added"],1)
        with session_scope(self.root) as s:
            fields=read_mapping(s,self.out["run_id"])["review_fields"]
            self.assertEqual(next(x for x in fields if x["field_name"]=="title")["value"],"桌边自粘垃圾袋")
            self.assertEqual(next(x for x in fields if x["field_name"]=="category")["source_kind"],"AI推测")


if __name__=="__main__":unittest.main()
