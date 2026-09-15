"""向现有爆款推荐页写入一条明确标记的“椅子”模拟演示数据。

候选只从当前稳定池中筛选，绝不新造供应商商品；爆款信号本身是演示用
需求卡，方便验证“稳定池 → 爆款搜索池 → 匹配评分”的完整链路。
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pintan.data_store import SEARCH_FILE, STABLE_FILE, TREND_FILE, atomic_write_csv, read_csv

TREND_ID = "TR-MOCK-CHAIR-20260915"


def positive(value: str) -> bool:
    try:
        return Decimal(str(value).strip()) > 0
    except (InvalidOperation, ValueError):
        return False


def numeric_display(value: str) -> str:
    """将“1个起批”“2年”等页面展示归一化为首个数值；空值保持空。"""
    match = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return match.group(0) if match else ""


def chair_rows(root: Path) -> list[dict[str, str]]:
    rows = [row for row in read_csv(root / STABLE_FILE)
            if "椅" in row.get("title", "") and positive(row.get("wholesale_price_cny", ""))]
    rows.sort(key=lambda row: (-float(row.get("total_score") or 0), row.get("source_supply_id", "")))
    if len(rows) < 10:
        raise ValueError("稳定池中采购价完整的椅子不足10条，无法创建爆款演示搜索池")
    return rows[:10]


def seed(root: Path) -> dict[str, object]:
    selected = chair_rows(root)
    trends = [row for row in read_csv(root / TREND_FILE) if row.get("trend_id") != TREND_ID]
    trends.append({
        "trend_id": TREND_ID,
        "platform": "模拟演示",
        "verification_status": "模拟需求，非平台实测",
        "data_origin": "mock_demo_from_stable_pool",
        "field_provenance": "爆款需求卡为模拟；候选商品均映射自当前稳定池",
        "observed_date": "2026-09-15",
        "title": "【模拟演示】人体工学办公椅爆款需求",
        "category": "家具/办公椅",
        "displayed_sales_range": "",
        "retail_price_cny": "399",
        "core_features": "人体工学|腰部支撑|透气网布|可调节",
        "target_audience": "久坐办公人群|学生",
        "use_scenarios": "办公室|书房|宿舍",
        "negative_review_pain_points": "腰部支撑不足|久坐闷热|安装复杂|尺寸不适配",
        "pain_point_evidence_summary": "模拟需求卡，仅用于验证爆款匹配流程；不代表真实平台销量或评价。",
        "search_keyword": "人体工学办公椅",
        "source_entry_url": "mock://chair-trend-demo",
    })

    all_candidates = [row for row in read_csv(root / SEARCH_FILE) if row.get("trend_id") != TREND_ID]
    for index, stable in enumerate(selected, 1):
        tags = stable.get("feature_tags", "")[:500]
        all_candidates.append({
            "search_id": f"SR-{TREND_ID}-{index:02d}",
            "trend_id": TREND_ID,
            "search_keyword": "人体工学办公椅",
            "title": stable["title"],
            "category": stable.get("category") or "家具/办公椅（标题归类，待复核）",
            "wholesale_price_cny": stable["wholesale_price_cny"],
            "estimated_shipping_cny": numeric_display(stable.get("estimated_shipping_cny", "")),
            "material": stable.get("material", ""),
            "feature_tags": tags,
            "moq": numeric_display(stable.get("moq", "")),
            "sales_30d": numeric_display(stable.get("sales_30d", "")),
            "repurchase_rate_pct": numeric_display(stable.get("repurchase_rate_pct", "")),
            "supplier_years": numeric_display(stable.get("supplier_years", "")),
            "supplier_quality_proxy_pct": numeric_display(stable.get("supplier_quality_proxy_pct", "")),
            "battery_capacity_mah": "",
            "power_w": "",
            "offer_url": stable.get("canonical_source_url") or stable.get("product_url", ""),
            "source_supply_id": stable["source_supply_id"],
            "source_platform": "1688",
            "data_origin": "stable_pool_mock_demo",
            "field_provenance": "标题、采购价、材质、功能标签及供应商字段均复制自稳定池；未补造缺失值",
            "verification_status": "稳定池已入池记录；用于模拟爆款匹配演示",
            "stable_score_snapshot": stable.get("total_score", ""),
        })

    atomic_write_csv(root / TREND_FILE, trends, list(dict.fromkeys(key for row in trends for key in row)))
    atomic_write_csv(root / SEARCH_FILE, all_candidates,
                     list(dict.fromkeys(key for row in all_candidates for key in row)))
    return {"trend_id": TREND_ID, "candidate_count": len(selected),
            "source_product_ids": [row["source_supply_id"] for row in selected]}


if __name__ == "__main__":
    import sys
    print(json.dumps(seed(Path.cwd()), ensure_ascii=False))
