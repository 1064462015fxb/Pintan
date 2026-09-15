"""为实时搜索结果补充可从标题确定性推导的匹配字段。

本脚本不伪造运费、MOQ、复购率、店铺年限或供应商质量。
这些不可从标题获得的字段继续保持空值。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from sqlalchemy import select

from pintan.database import BusinessRow, SearchCandidate, session_scope

ROOT = Path(__file__).resolve().parents[1]

VOCAB = {
    "家居/坐垫靠垫": ["人体工学", "护腰", "坐垫", "靠背", "一体", "记忆棉", "加大", "腰部支撑"],
    "家纺/枕芯": ["记忆棉", "慢回弹", "护颈", "U型", "便携", "凉感", "零压", "酒店", "侧睡", "按摩"],
    "个护家清/驱蚊用品": ["羟哌酯", "喷雾", "便携", "户外", "无香", "倒喷", "15%", "20%", "橘子味"],
}


def tags(category: str, title: str) -> str:
    found = [word for word in VOCAB.get(category, []) if word.lower() in title.lower()]
    return "|".join(found or [category.split("/")[-1]])


def sales_floor(value: str) -> int:
    """保留展示口径的最低值，例如“10万+件”转为100000。"""
    text = (value or "").replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", text)
    if not match:
        return 0
    number = float(match.group(1))
    return int(number * 10_000 if "万" in text else number)


def main() -> None:
    changed = 0
    with session_scope(ROOT) as session:
        rows = list(session.scalars(select(BusinessRow).where(BusinessRow.kind == "search")))
        for business in rows:
            row = json.loads(business.payload)
            if not row.get("feature_tags"):
                row["feature_tags"] = tags(row.get("category", ""), row.get("title", ""))
                row["field_provenance"] = (row.get("field_provenance", "") +
                    "；feature_tags=仅从商品标题确定性抽取").lstrip("；")
                changed += 1
            displayed = row.get("displayed_sales_range") or row.get("sales_30d", "")
            row["displayed_sales_range"] = displayed
            row["sales_30d"] = str(sales_floor(displayed))
            row["sales_metric_note"] = "1688搜索页展示成交量的数值下界，非近30天成交"
            # 从标题只能确认明示材质，不做行业常识猜测。
            if not row.get("material") and "记忆棉" in row.get("title", ""):
                row["material"] = "记忆棉（标题明示，待详情复核）"
            business.payload = json.dumps(row, ensure_ascii=False)
            candidate = session.get(SearchCandidate, row["search_id"])
            if candidate:
                candidate.payload = json.dumps(row, ensure_ascii=False)
    print(f"enriched search candidates={changed}")


if __name__ == "__main__":
    main()
