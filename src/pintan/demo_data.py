"""从两张旧演示输入生成三张新业务表；已有目标文件时绝不覆盖。"""
from __future__ import annotations

from pathlib import Path

from .data_store import SEARCH_FILE, STABLE_FILE, TREND_FILE, atomic_write_csv, read_csv
from .models import split_tags


SCORE_FIELDS = ["heat_score", "competition_score", "margin_score", "differentiation_score", "trust_score", "total_score", "stable_rank", "score_version", "scored_at"]
MATCH_FIELDS = ["feature_match_score", "price_advantage_score", "pain_resolution_score", "supplier_quality_score", "match_score", "match_rank", "recommendation", "recommendation_reason", "resolved_pain_points", "unresolved_pain_points", "risk_flags", "evidence_fields", "agent_status", "model", "prompt_version", "matched_at"]


def _similarity(trend: dict[str, str], supply: dict[str, str]) -> tuple[int, str]:
    wanted, offered = set(split_tags(trend.get("core_features", ""))), set(split_tags(supply.get("feature_tags", "")))
    overlap = len(wanted & offered)
    title_hit = int(any(token and token in supply.get("title", "") for token in trend.get("search_keyword", "").replace("风扇", "").split()))
    return overlap * 10 + title_hit, supply["supply_id"]


def initialize_demo(root: Path) -> dict[str, int]:
    stable_path, search_path = root / STABLE_FILE, root / SEARCH_FILE
    existing = [p.name for p in (stable_path, search_path) if p.exists()]
    if existing:
        raise FileExistsError(f"拒绝覆盖已有业务表: {', '.join(existing)}")
    source = read_csv(root / "pintan_1688_supply_candidates.csv")
    trends = read_csv(root / TREND_FILE)
    stable = []
    for item in source:
        row = dict(item)
        row["stable_id"] = row.pop("supply_id")
        landed = float(row["wholesale_price_cny"]) + float(row["estimated_shipping_cny"])
        row["reference_retail_price_cny"] = f"{landed * 2:.2f}"
        row["reference_price_provenance"] = "演示假设:到手成本x2"
        row.update({key: "" for key in SCORE_FIELDS})
        stable.append(row)
    atomic_write_csv(stable_path, stable)

    searches = []
    for trend in trends:
        ranked = sorted(source, key=lambda s: (-_similarity(trend, s)[0], _similarity(trend, s)[1]))[:10]
        for index, supply in enumerate(ranked, 1):
            row = {
                "search_id": f"SR-{trend['trend_id']}-{index:02d}", "trend_id": trend["trend_id"],
                "source_supply_id": supply["supply_id"], "data_origin": "model_processed_demo",
                "field_provenance": "由现有1688演示候选确定性派生；非实时采集",
                "source_platform": "1688", "verification_status": "待复核",
                "search_keyword": trend.get("search_keyword", ""), "search_entry_url": supply.get("source_query_url", ""),
                "offer_url": "", "collected_at": "", "category": supply["category"], "title": supply["title"],
                "wholesale_price_cny": supply["wholesale_price_cny"], "estimated_shipping_cny": supply["estimated_shipping_cny"],
                "moq": supply["moq"], "sales_30d": supply["sales_30d"], "repurchase_rate_pct": supply["repurchase_rate_pct"],
                "supplier_years": supply["supplier_years"], "supplier_quality_proxy_pct": supply["supplier_quality_proxy_pct"],
                "material": supply["material"], "battery_capacity_mah": supply["battery_capacity_mah"],
                "power_w": supply["power_w"], "feature_tags": supply["feature_tags"], "color_options": supply["color_options"],
            }
            row.update({key: "" for key in MATCH_FIELDS}); searches.append(row)
    atomic_write_csv(search_path, searches)
    return {"stable_products": len(stable), "trends": len(trends), "search_results": len(searches)}

