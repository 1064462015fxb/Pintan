from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pintan.agent import apply_explanation
from pintan.models import SelectionResult
from pintan.repository import load_supplies, load_trends, validate_inputs
from pintan.scoring import rank_candidates, score_pair


ROOT = Path(__file__).resolve().parents[1]


class DataAndScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.supplies = load_supplies(ROOT / "pintan_1688_supply_candidates.csv")
        cls.trends = load_trends(ROOT / "pintan_market_trending_products.csv")

    def test_inputs_validate_and_allow_platform_specific_blanks(self):
        self.assertEqual(validate_inputs(self.supplies, self.trends), {"supplies": 30, "trends": 20, "taobao": 10, "douyin": 10})
        self.assertTrue(any(not x.raw.get("likes_count") for x in self.trends if x.platform == "淘宝"))
        self.assertTrue(any(not x.raw.get("favorites_count") for x in self.trends if x.platform == "抖音"))

    def test_ranking_is_deterministic(self):
        first = rank_candidates("RUN-X", self.trends[0], self.supplies, 3)
        second = rank_candidates("RUN-X", self.trends[0], self.supplies, 3)
        self.assertEqual([(x.supply_id, x.match_score) for x in first], [(x.supply_id, x.match_score) for x in second])
        self.assertEqual([1, 2, 3], [x.rank_within_trend for x in first])

    def test_locked_formulas(self):
        row = score_pair("RUN-X", self.trends[0], self.supplies[0])
        expected_match = round(row.price_advantage_score * .40 + row.feature_similarity_score * .25 + row.pain_point_resolution_score * .25 + row.supplier_quality_score * .10, 1)
        expected_total = round(row.heat_score * .25 + row.competition_score * .20 + row.margin_score * .25 + row.gap_score * .20 + row.trust_score * .10, 1)
        self.assertEqual(row.match_score, expected_match)
        self.assertEqual(row.total_score, expected_total)

    def test_lower_drivers_lower_match(self):
        row = score_pair("RUN-X", self.trends[0], self.supplies[0])
        lower = (row.price_advantage_score - 10) * .40 + row.feature_similarity_score * .25 + (row.pain_point_resolution_score - 10) * .25 + (row.supplier_quality_score - 10) * .10
        self.assertLess(lower, row.match_score)

    def test_agent_cannot_change_scores_and_must_cite_evidence(self):
        row = score_pair("RUN-X", self.trends[0], self.supplies[0])
        locked = (row.match_score, row.total_score, row.recommendation)
        apply_explanation(row, {
            "recommendation_reason": "折叠和数显对应趋势特征，但续航仍需验证。",
            "resolved_pain_points": ["档位切换不直观"], "unresolved_pain_points": ["续航虚标"],
            "risk_flags": ["数据待复核"], "evidence_fields": ["trend.core_features", "supply.feature_tags"],
        }, allowed_values="折叠 数显 续航")
        self.assertEqual(locked, (row.match_score, row.total_score, row.recommendation))
        self.assertEqual(row.agent_status, "success")

    def test_invalid_evidence_is_rejected(self):
        row = score_pair("RUN-X", self.trends[0], self.supplies[0])
        with self.assertRaises(ValueError):
            apply_explanation(row, {"recommendation_reason": "无依据", "evidence_fields": ["invented.field"]}, "")


if __name__ == "__main__":
    unittest.main()

