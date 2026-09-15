"""真实搜索数据缺失时的保守校验。"""
from __future__ import annotations

import unittest
from pathlib import Path

from pintan.trending import prepare_input, validate_compact


ROOT = Path(__file__).resolve().parents[1]
TREND_ID = "TR-REAL-TB-669170574398"


class RealTrendingInputTests(unittest.TestCase):
    def assessments(self):
        payload = prepare_input(ROOT, TREND_ID)["payload"]
        pains = [x for x in payload["trend"]["negative_review_pain_points"].split("|") if x]
        rows = []
        for candidate in payload["candidates"]:
            rows.append({
                "search_id": candidate["search_id"],
                "feature_match_score": 50, "price_advantage_score": 0,
                "pain_resolution_score": 0, "supplier_quality_score": 0,
                "recommendation": "谨慎测试", "recommendation_reason": "证据缺失，仅核验特征匹配",
                "evidence_fields": ["trend.title", "search.title"],
                "resolved_pain_points": [], "unresolved_pain_points": pains,
            })
        return payload, rows

    def test_real_inputs_are_accepted_without_fabricated_numeric_values(self):
        prepared = prepare_input(ROOT, TREND_ID)
        self.assertEqual(len(prepared["candidates"]), 10)
        self.assertTrue(all(row["feature_tags"] for row in prepared["candidates"]))

    def test_missing_retail_and_supplier_evidence_force_zero_scores(self):
        payload, rows = self.assessments()
        self.assertEqual(len(validate_compact({"assessments": rows}, payload)), 10)
        rows[0]["price_advantage_score"] = 1
        with self.assertRaisesRegex(ValueError, "price_advantage_score"):
            validate_compact({"assessments": rows}, payload)


if __name__ == "__main__":
    unittest.main()
