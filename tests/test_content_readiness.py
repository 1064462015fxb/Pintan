"""内容生产闭环的前置门禁；不调用浏览器或付费模型。"""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from pintan.collector import collect_snapshot
from pintan.content_readiness import readiness
from pintan.database import (Base, CollectorTask, ProductAsset, engine, session_scope,
                             utcnow)
from pintan.webapp import create_app

ROOT = Path(__file__).resolve().parents[1]


def block(region, text):
    return {"region": region, "text": text, "source_locator": "fixture." + region}


class ContentReadinessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
        for name in ["pintan_stable_products.csv", "pintan_market_trending_products.csv", "pintan_trend_search_results.csv"]:
            shutil.copy2(ROOT / name, self.root / name)
        Base.metadata.create_all(engine(self.root))
        self.run = collect_snapshot(self.root, {
            "page_url": "https://detail.1688.com/offer/123.html", "title": "桌边垃圾袋",
            "text_blocks": [block("title", "桌边垃圾袋"),
                block("attributes", "类目：垃圾袋\n材质：PE"), block("price", "起批量：1包")],
            "images": [{"url": "https://cbu01.alicdn.com/a.jpg", "region": "主图"}]
        }, False)
        folder = self.root / "data/assets/1688-123/source"; folder.mkdir(parents=True)
        (folder / "a.jpg").write_bytes(b"fixture")
        with session_scope(self.root) as s:
            asset = s.query(ProductAsset).filter(ProductAsset.product_id == "1688-123").first()
            asset.local_path = "data/assets/1688-123/source/a.jpg"; asset.sha256 = "abc"
            asset.width = 800; asset.height = 800; asset.status = "待授权确认"
            s.add(CollectorTask(task_id="CT-assets", helper_id="", offer_id="123",
                page_url="https://detail.1688.com/offer/123.html", status="partial",
                payload=json.dumps({"mode":"assets", "run_id":self.run["run_id"], "found":1,
                                    "downloaded":1, "failed":0}, ensure_ascii=False),
                created_at=utcnow(), updated_at=utcnow()))
        self.client = TestClient(create_app(self.root), base_url="http://127.0.0.1:8010")

    def tearDown(self): self.tmp.cleanup()

    def test_facts_then_product_authorization_unlocks_content(self):
        before = readiness(self.root, "1688-123")
        self.assertEqual(before["next_action"], "review_facts")
        self.assertFalse(before["ready"]); self.assertEqual(before["assets"]["usable"], 1)
        blocked = self.client.post("/api/tasks/content/diagnose", json={"product_id":"1688-123"})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(blocked.json()["detail"]["code"], "content_not_ready")

        confirmed = self.client.post("/api/content/products/1688-123/confirm-facts",
                                     json={"run_id":self.run["run_id"]})
        self.assertEqual(confirmed.status_code, 200)
        state = confirmed.json()["readiness"]
        self.assertTrue(state["facts"]["confirmed"]); self.assertEqual(state["next_action"], "authorize_assets")

        asset_id = state["assets"]["items"][0]["asset_id"]
        authorized = self.client.post("/api/content/products/1688-123/authorize-assets", json={
            "confirmed":True, "statement":"我已确认拥有该商品当前所选图片的使用权",
            "asset_ids":[asset_id]})
        self.assertEqual(authorized.status_code, 200)
        self.assertTrue(authorized.json()["readiness"]["ready"])

    def test_hash_change_invalidates_product_authorization(self):
        self.client.post("/api/content/products/1688-123/confirm-facts", json={"run_id":self.run["run_id"]})
        state = readiness(self.root, "1688-123"); asset_id = state["assets"]["items"][0]["asset_id"]
        self.client.post("/api/content/products/1688-123/authorize-assets", json={
            "confirmed":True, "statement":"我已确认拥有该商品当前所选图片的使用权", "asset_ids":[asset_id]})
        with session_scope(self.root) as s:s.get(ProductAsset, asset_id).sha256 = "changed"
        state = readiness(self.root, "1688-123")
        self.assertFalse(state["ready"]); self.assertEqual(state["assets"]["authorized"], 0)

    def test_asset_can_be_reclassified_or_excluded_after_fact_confirmation(self):
        self.client.post("/api/content/products/1688-123/confirm-facts",
                         json={"run_id":self.run["run_id"]})
        state = readiness(self.root, "1688-123")
        asset_id = state["assets"]["items"][0]["asset_id"]
        changed = self.client.post(
            f"/api/collector/runs/{self.run['run_id']}/assets/{asset_id}/review",
            json={"asset_type":"详情图", "included":True})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(readiness(self.root, "1688-123")["assets"]["items"][0]["asset_type"], "详情图")
        excluded = self.client.post(
            f"/api/collector/runs/{self.run['run_id']}/assets/{asset_id}/review",
            json={"asset_type":"详情图", "included":False})
        self.assertEqual(excluded.status_code, 200)
        self.assertEqual(readiness(self.root, "1688-123")["assets"]["usable"], 0)


if __name__ == "__main__": unittest.main()
