"""优化流程的离线契约测试：无真实模型、无网络，使用临时三表。"""
from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from fastapi.testclient import TestClient

from pintan import trending as fast
from pintan.data_store import SEARCH_FILE, STABLE_FILE, TREND_FILE, atomic_write_csv, read_csv
from pintan.demo_data import initialize_demo
from pintan.webapp import create_app

ROOT = Path(__file__).resolve().parents[1]


class FastMatchingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("pintan_1688_supply_candidates.csv", TREND_FILE): shutil.copy2(ROOT / name, self.root / name)
        shutil.copytree(ROOT / "skills", self.root / "skills")
        initialize_demo(self.root)
        self.env = patch.dict(os.environ, {"PINTAN_MODEL_PROVIDER": "qwen", "PINTAN_TRENDING_MODEL": "qwen3.7-plus"}, clear=True)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def records(self, payload):
        pains = list(filter(None, payload["trend"]["negative_review_pain_points"].split("|")))
        return [{"search_id": r["search_id"], "feature_match_score": 60, "price_advantage_score": 70,
            "pain_resolution_score": 50, "supplier_quality_score": 80, "recommendation": "谨慎测试",
            "recommendation_reason": "功能部分对应，痛点仍待核验。", "resolved_pain_points": [],
            "unresolved_pain_points": pains, "evidence_fields": ["trend.core_features", "search.feature_tags"]}
            for r in payload["candidates"]]

    def fake_invoke(self, root, prompt, validate, mode):
        if prompt["mode"] == "compact_matching":
            value = {"assessments": self.records(prompt)}
        else:
            value = {"explanations": [{"search_id": c["search_id"], "explanation": "功能标签对应部分需求，实际效果待复核。",
                "evidence_fields": ["trend.core_features", "search.feature_tags"]} for c in prompt["candidates"]]}
        return validate(value), {"skills_loaded": ["trending-selection"], "skill_read": True, "tools_called": ["read_file"]}, {
            "model_calls": 2, "input_tokens": 100, "output_tokens": 100, "corrections": 0}

    def run_match(self):
        with patch.object(fast, "_invoke", side_effect=self.fake_invoke):
            return fast.run_trending_agent(self.root, "TR-DY-001")

    def test_formula_rounding_and_model_cannot_supply_total(self):
        prepared = fast.prepare_input(self.root, "TR-DY-001")
        records = self.records(prepared["payload"])
        self.assertEqual(fast.weighted_score(records[0]), 63.5)
        self.assertEqual(fast.weighted_score({k: 0.05 for k in fast.WEIGHTS}), 0.1)
        records[0]["match_score"] = 100
        with self.assertRaises(ValueError): fast.validate_compact({"assessments": records}, prepared["payload"])

    def test_success_cache_hit_no_model_and_original_time(self):
        first = self.run_match()
        with patch.object(fast, "_invoke", side_effect=AssertionError("cache must not call model")):
            second = fast.run_trending_agent(self.root, "TR-DY-001")
        self.assertTrue(second["cache_hit"])
        self.assertEqual(second["generated_at"], first["generated_at"])
        self.assertEqual(second["run_id"], first["run_id"])
        self.assertEqual(second["metrics"]["model_calls"], 0)
        self.assertEqual(second["all10"], first["all10"])
        self.assertEqual([r["search_id"] for r in second["all10"]], sorted(r["search_id"] for r in second["all10"]))

    def test_force_calls_model_and_gets_new_run(self):
        first = self.run_match()
        with patch.object(fast, "_invoke", side_effect=self.fake_invoke) as mocked:
            second = fast.run_trending_agent(self.root, "TR-DY-001", force=True)
        mocked.assert_called_once()
        self.assertFalse(second["cache_hit"])
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_key_excludes_order_scores_times_and_secret(self):
        before = fast.prepare_input(self.root, "TR-DY-001")["key"]
        rows = read_csv(self.root / SEARCH_FILE)
        for row in rows:
            row["match_score"] = "99"
            row["collected_at"] = "a metadata-only timestamp"
        atomic_write_csv(self.root / SEARCH_FILE, list(reversed(rows)))
        os.environ["QWEN_API_KEY"] = "placeholder-not-real"
        self.assertEqual(before, fast.prepare_input(self.root, "TR-DY-001")["key"])

    def test_key_changes_with_facts_model_and_skill(self):
        original = fast.prepare_input(self.root, "TR-DY-001")["key"]
        for field, value in (("wholesale_price_cny", "99"), ("feature_tags", "new-feature")):
            rows = read_csv(self.root / SEARCH_FILE)
            row = next(r for r in rows if r["trend_id"] == "TR-DY-001")
            old = row[field]; row[field] = value; atomic_write_csv(self.root / SEARCH_FILE, rows)
            self.assertNotEqual(original, fast.prepare_input(self.root, "TR-DY-001")["key"])
            row[field] = old; atomic_write_csv(self.root / SEARCH_FILE, rows)
        os.environ["PINTAN_TRENDING_MODEL"] = "qwen3.6-flash"
        self.assertNotEqual(original, fast.prepare_input(self.root, "TR-DY-001")["key"])
        os.environ["PINTAN_TRENDING_MODEL"] = "qwen3.7-plus"
        with patch.object(fast, "PROMPT_VERSION", "next-version"):
            self.assertNotEqual(original, fast.prepare_input(self.root, "TR-DY-001")["key"])
        skill = self.root / "skills/trending-selection/SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8") + "\nnew rule", encoding="utf-8")
        self.assertNotEqual(original, fast.prepare_input(self.root, "TR-DY-001")["key"])

    def test_failure_does_not_cache_or_replace_success(self):
        first = self.run_match()
        old_csv = (self.root / SEARCH_FILE).read_bytes()
        with patch.object(fast, "_invoke", side_effect=ValueError("invalid model output")):
            with self.assertRaises(ValueError): fast.run_trending_agent(self.root, "TR-DY-001", force=True)
        self.assertEqual(old_csv, (self.root / SEARCH_FILE).read_bytes())
        self.assertEqual(fast.latest_match(self.root, "TR-DY-001")["run_id"], first["run_id"])
        self.assertEqual(len(list((self.root / "outputs/match_cache").glob('*.json'))), 1)

    def test_missing_candidate_and_invalid_numeric(self):
        rows = read_csv(self.root / SEARCH_FILE)
        target = next(r for r in rows if r["trend_id"] == "TR-DY-001")
        target["wholesale_price_cny"] = "NaN"; atomic_write_csv(self.root / SEARCH_FILE, rows)
        with self.assertRaises(ValueError): fast.prepare_input(self.root, "TR-DY-001")
        rows.remove(target); atomic_write_csv(self.root / SEARCH_FILE, rows)
        with self.assertRaises(ValueError): fast.prepare_input(self.root, "TR-DY-001")

    def test_output_integrity(self):
        payload = fast.prepare_input(self.root, "TR-DY-001")["payload"]
        good = self.records(payload)
        for mutation in (lambda r: r.pop(), lambda r: r[0].update(search_id=r[1]["search_id"]),
            lambda r: r[0].update(evidence_fields=["trend.imaginary", "search.feature_tags"]),
            lambda r: r[0].update(unresolved_pain_points=["invented"]),
            lambda r: r[0].update(recommendation_reason="长"*61)):
            records = copy.deepcopy(good); mutation(records)
            with self.assertRaises(ValueError): fast.validate_compact({"assessments": records}, payload)

    def test_detail_failure_and_success_do_not_modify_scores(self):
        match = self.run_match(); before = (self.root / SEARCH_FILE).read_bytes()
        with patch.object(fast, "_invoke", side_effect=ValueError("bad details")):
            with self.assertRaises(ValueError): fast.run_top3_explanations(self.root, "TR-DY-001")
        self.assertEqual(before, (self.root / SEARCH_FILE).read_bytes())
        with patch.object(fast, "_invoke", side_effect=self.fake_invoke):
            details = fast.run_top3_explanations(self.root, "TR-DY-001")
        self.assertEqual(before, (self.root / SEARCH_FILE).read_bytes())
        self.assertEqual(details["match_run_id"], match["run_id"])
        with patch.object(fast, "_invoke", side_effect=AssertionError("must use detail cache")):
            self.assertTrue(fast.run_top3_explanations(self.root, "TR-DY-001")["cache_hit"])
        with patch.object(fast, "_invoke", side_effect=self.fake_invoke):
            new = fast.run_trending_agent(self.root, "TR-DY-001", force=True)
        self.assertIsNone(fast.read_details(self.root, new))

    def test_api_cache_force_details_and_stale(self):
        client = TestClient(create_app(self.root))
        with patch.object(fast, "_invoke", side_effect=self.fake_invoke):
            task = client.post('/api/tasks/trending/TR-DY-001').json()["task_id"]
            self.assertEqual(client.get('/api/tasks/'+task).json()["status"], "completed")
        data = client.get('/api/trends/TR-DY-001').json()
        self.assertEqual(len(data["top3"]), 3)
        self.assertFalse(data["metadata"]["cache_hit"])
        with patch.object(fast, "_invoke", side_effect=self.fake_invoke) as mocked:
            client.post('/api/tasks/trending/TR-DY-001?force=true')
            mocked.assert_called_once()
            detail_task = client.post('/api/tasks/trending/TR-DY-001/explanations').json()["task_id"]
        self.assertEqual(client.get('/api/tasks/'+detail_task).json()["status"], "completed")
        self.assertIsNotNone(client.get('/api/trends/TR-DY-001').json()["details"])
        with patch.object(fast, "PROMPT_VERSION", "changed"):
            self.assertEqual(client.post('/api/tasks/trending/TR-DY-001/explanations').status_code, 409)
            self.assertIsNone(client.get('/api/trends/TR-DY-001').json()["details"])
        self.assertEqual(client.get('/api/tasks/missing').status_code, 404)

    def test_queued_task_deduplication(self):
        app = create_app(self.root); background = BackgroundTasks()
        first = app.state.submit_task(background, "trending", "TR-DY-001")
        again = app.state.submit_task(background, "trending", "TR-DY-001")
        self.assertEqual(first["task_id"], again["task_id"])
        self.assertTrue(again["deduplicated"])
        self.assertEqual(len(background.tasks), 1)
        with self.assertRaises(HTTPException): app.state.submit_task(background, "trending", "TR-DY-001", True)


if __name__ == "__main__": unittest.main()
