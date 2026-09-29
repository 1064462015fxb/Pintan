"""跨平台爆款发现、证据评分与1688平替门禁。

本模块只计算页面证据，不把模型推测写入销量、评论或排名字段。
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import delete, select

from .database import (BusinessRow, Product, TrendDiscoveryBatch, TrendDiscoveryCandidate,
    TrendDiscoverySource, TrendSignal, read_business, session_scope, write_business)

PLATFORMS = ("taobao", "jd", "douyin")
PLATFORM_NAMES = {"taobao": "淘宝", "jd": "京东", "douyin": "抖音"}
DEFAULT_FILTERS = {
    "platforms": list(PLATFORMS), "price_min": None, "price_max": None,
    "min_sales": None, "min_engagement": None,
    "exclude_words": ["教程", "课程", "二手", "虚拟", "代拍"],
    "exclude_ads": True, "trend_score_threshold": 70,
    "evidence_coverage_threshold": 60, "max_per_platform": 20,
}
SEARCH_URLS = {
    "taobao": "https://s.taobao.com/search?q={query}",
    "jd": "https://search.jd.com/Search?keyword={query}",
    "douyin": "https://www.douyin.com/search/{query}?type=product",
    "douyin_buyin": "https://buyin.jinritemai.com/dashboard/service-hall/daren-square?keyword={query}",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_id(prefix: str, *parts: str) -> str:
    value = "|".join(str(x) for x in parts)
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]}"


def parse_lower_bound(value: Any) -> float | None:
    """解析页面展示下限；100+、1万+均保留为下限而非精确值。"""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).replace(",", "").strip().lower()
    if not text or text in {"--", "未知", "暂无"}:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)\s*([万w千k]?)", text, re.I)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2).lower()
    return number * ({"万": 10000, "w": 10000, "千": 1000, "k": 1000}.get(unit, 1))


def normalize_filters(value: dict[str, Any] | None) -> dict[str, Any]:
    result = {**DEFAULT_FILTERS, **(value or {})}
    result["platforms"] = [x for x in result.get("platforms", []) if x in PLATFORMS]
    if not result["platforms"]:
        raise ValueError("至少选择一个平台")
    result["max_per_platform"] = max(1, min(20, int(result.get("max_per_platform") or 20)))
    for key in ("trend_score_threshold", "evidence_coverage_threshold"):
        result[key] = max(0, min(100, float(result.get(key, DEFAULT_FILTERS[key]))))
    excludes = result.get("exclude_words") or []
    result["exclude_words"] = [str(x).strip() for x in excludes if str(x).strip()][:30]
    return result


def suggest_filters(category: str, root: Path | None = None) -> dict[str, Any]:
    """AI只建议可验证过滤条件；失败时明确回退到安全默认值。"""
    category = category.strip()
    if not category:
        raise ValueError("请输入品类")
    result={"category": category, "suggested_query": category, "source":"rules-fallback",
        "filters": normalize_filters(None),
        "explanation": "先广泛读取三个平台各20个可见候选，再按本批次相对热度和证据覆盖率筛选。"}
    if root is not None:
        try:
            from .batch_selection import recommend_filters
            advice=recommend_filters(root,category)
            result.update(suggested_query=advice.get("normalized_keyword") or category,
                source=advice.get("source","rules-fallback"),model=advice.get("model"))
            result["filters"].update(price_min=advice.get("price_min"),price_max=advice.get("price_max"),
                min_sales=advice.get("min_sales"),exclude_words=advice.get("exclude_keywords") or result["filters"]["exclude_words"])
            result["explanation"]=advice.get("rationale") or result["explanation"]
        except Exception as exc:
            result["error_type"]=type(exc).__name__
    return result


def product_key(platform: str, url: str, supplied: str = "") -> str:
    if supplied:
        return re.sub(r"[^a-zA-Z0-9_-]", "", supplied)[:120] or stable_id("item", url)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("id", "sku", "wareId", "product_id", "item_id"):
        if query.get(key):
            return query[key][0]
    ids = re.findall(r"(?<!\d)(\d{6,})(?!\d)", parsed.path)
    return ids[-1] if ids else stable_id("item", platform, parsed.netloc, parsed.path)


def _title_key(title: str) -> str:
    value = re.sub(r"[\W_]+", "", title.lower(), flags=re.UNICODE)
    return re.sub(r"(?:淘宝|天猫|京东|抖音|同款|旗舰店|官方)", "", value)[:80]


def _percentiles(rows: list[dict[str, Any]], field: str) -> dict[str, float]:
    present = [(r["source_id"], r.get(field)) for r in rows if r.get(field) is not None]
    if not present:
        return {}
    ordered = sorted(present, key=lambda item: (float(item[1]), item[0]))
    if len(ordered) == 1:
        return {ordered[0][0]: 100.0}
    result: dict[str, float] = {}
    for index, (identity, value) in enumerate(ordered):
        first = next(i for i, (_, x) in enumerate(ordered) if x == value)
        last = len(ordered) - 1 - next(i for i, (_, x) in enumerate(reversed(ordered)) if x == value)
        result[identity] = round(((first + last) / 2) / (len(ordered) - 1) * 100, 1)
    return result


def normalize_source(platform: str, raw: dict[str, Any], rank: int) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    url = str(raw.get("url") or raw.get("page_url") or "").strip()
    sales_text = str(raw.get("sales_text") or "").strip()
    engagement_text = str(raw.get("engagement_text") or raw.get("reviews_text") or "").strip()
    price_text = str(raw.get("price_text") or raw.get("price") or "").strip()
    cover_url = str(raw.get("cover_url") or raw.get("image_url") or "").strip()
    key = product_key(platform, url, str(raw.get("product_id") or ""))
    evidence=raw.get("evidence") if isinstance(raw.get("evidence"),dict) else {}
    return {
        "source_id": stable_id("TDS", platform, key), "platform": platform,
        "product_key": key, "title": title, "title_key": _title_key(title), "url": url,
        "price_text": price_text, "price_value": parse_lower_bound(price_text),
        "sales_text": sales_text, "sales_value": parse_lower_bound(sales_text),
        "engagement_text": engagement_text, "engagement_value": parse_lower_bound(engagement_text),
        "rank": max(1, min(20, int(raw.get("rank") or rank))), "shop": str(raw.get("shop") or "").strip(),
        "cover_url": cover_url, "is_ad": bool(raw.get("is_ad")),
        "hard_block": str(raw.get("hard_block") or "").strip(),
        "specs_text":str(raw.get("specs_text") or "")[:3000],
        "selling_points":str(raw.get("selling_points") or "")[:2000],
        "review_snippets":[str(x)[:500] for x in (raw.get("review_snippets") or [])[:20]],
        "detail_status":str(raw.get("detail_status") or "未补充")[:100],
        "evidence": evidence, "capture_method": raw.get("capture_method") or "visible_dom",
    }


def _passes_filters(row: dict[str, Any], filters: dict[str, Any]) -> tuple[bool, str]:
    if not row["title"] or not row["url"]:
        return False, "缺少标题或详情链接"
    try:host=urlparse(row['url']).hostname or ''
    except ValueError:return False,'详情链接无效'
    allowed={'taobao':('item.taobao.com','detail.tmall.com'),'jd':('item.jd.com',),
        'douyin':('www.douyin.com','haohuo.jinritemai.com','buyin.jinritemai.com')}
    if not any(host==value or host.endswith('.'+value) for value in allowed.get(row['platform'],())):
        return False,'详情链接不属于对应平台'
    if filters.get("exclude_ads") and row.get("is_ad"):
        return False, "广告商品"
    if any(word.lower() in row["title"].lower() for word in filters.get("exclude_words", [])):
        return False, "命中标题排除词"
    price = row.get("price_value")
    if filters.get("price_min") not in (None, "") and (price is None or price < float(filters["price_min"])):
        return False, "低于价格下限或价格缺失"
    if filters.get("price_max") not in (None, "") and (price is None or price > float(filters["price_max"])):
        return False, "高于价格上限或价格缺失"
    if filters.get("min_sales") not in (None, "") and (row.get("sales_value") is None or row["sales_value"] < float(filters["min_sales"])):
        return False, "销量不足或缺失"
    if filters.get("min_engagement") not in (None, "") and (row.get("engagement_value") is None or row["engagement_value"] < float(filters["min_engagement"])):
        return False, "评论/互动不足或缺失"
    return True, ""


def score_sources(source_rows: list[dict[str, Any]], filters: dict[str, Any]) -> list[dict[str, Any]]:
    """按平台内百分位评分，再对高置信同标题跨平台合并。"""
    filters = normalize_filters(filters)
    accepted = []
    for row in source_rows:
        ok, reason = _passes_filters(row, filters)
        row = {**row, "filter_passed": ok, "filter_reason": reason}
        if ok:
            accepted.append(row)
    for platform in PLATFORMS:
        subset = [r for r in accepted if r["platform"] == platform]
        sales = _percentiles(subset, "sales_value")
        engagement = _percentiles(subset, "engagement_value")
        for row in subset:
            row["sales_percentile"] = sales.get(row["source_id"], 0)
            row["engagement_percentile"] = engagement.get(row["source_id"], 0)
            row["rank_score"] = round(max(0, (20 - row["rank"]) / 19 * 100), 1)
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in accepted:
        # 只有规范化标题完全一致才自动跨平台合并；疑似同款继续分开。
        key = row["title_key"] or f'{row["platform"]}:{row["product_key"]}'
        groups.setdefault(key, []).append(row)
    candidates = []
    for key, sources in groups.items():
        platform_count = len({x["platform"] for x in sources})
        cross = {1: 0.0, 2: 60.0}.get(platform_count, 100.0)
        sales = max(x.get("sales_percentile", 0) for x in sources)
        engagement = max(x.get("engagement_percentile", 0) for x in sources)
        rank_score = max(x.get("rank_score", 0) for x in sources)
        has_sales = any(x.get("sales_value") is not None for x in sources)
        has_engagement = any(x.get("engagement_value") is not None for x in sources)
        coverage = 35 + (40 if has_sales else 0) + (25 if has_engagement else 0)
        score = round(sales * .40 + engagement * .25 + rank_score * .20 + cross * .15, 1)
        best = max(sources, key=lambda x: (x.get("rank_score", 0), x.get("sales_value") or -1))
        blockers = []
        if not str(best.get("cover_url") or '').startswith('https://'): blockers.append("缺少有效主图")
        if not (has_sales or has_engagement): blockers.append("销量和评论/互动均缺失")
        if any(x.get("hard_block") for x in sources): blockers.append("存在明确阻断风险")
        qualified = score >= filters["trend_score_threshold"] and coverage >= filters["evidence_coverage_threshold"] and not blockers
        candidates.append({
            "candidate_id": stable_id("TDC", key), "title": best["title"], "category": "",
            "cover_url": best.get("cover_url", ""), "retail_price_cny": best.get("price_value"),
            "source_platforms": sorted({x["platform"] for x in sources}), "platform_count": platform_count,
            "trend_score": score, "evidence_coverage": float(coverage), "sales_score": sales,
            "engagement_score": engagement, "rank_score": rank_score, "cross_platform_score": cross,
            "status": "晋级" if qualified else "淘汰", "blockers": blockers,
            "merge_confidence": 1.0 if platform_count > 1 else None,
            "merge_basis": "规范化标题完全一致" if platform_count > 1 else "单平台商品",
            "sources": sources,
        })
    candidates.sort(key=lambda x: (-x["trend_score"], -x["evidence_coverage"], x["candidate_id"]))
    qualified_ids = {x["candidate_id"] for x in [c for c in candidates if c["status"] == "晋级"][:10]}
    for candidate in candidates:
        if candidate["status"] == "晋级" and candidate["candidate_id"] not in qualified_ids:
            candidate["status"] = "未进入总榜"
            candidate["blockers"].append("总榜最多保留10个")
    return candidates


def score_substitute(*, visual: float, feature: float, price: float | None, pain: float,
        supplier: float, trend_retail: float | None, landed_cost: float | None,
        threshold: float = 70) -> dict[str, Any]:
    for name, value in (("visual", visual), ("feature", feature), ("pain", pain), ("supplier", supplier)):
        if not 0 <= float(value) <= 100:
            raise ValueError(f"{name} 必须在0到100之间")
    price_score = 0.0 if price is None else max(0, min(100, float(price)))
    advantage = None
    if trend_retail and landed_cost is not None and trend_retail > 0:
        advantage = round((trend_retail - landed_cost) / trend_retail * 100, 1)
    total = round(visual * .30 + feature * .25 + price_score * .20 + pain * .15 + supplier * .10, 1)
    blockers = []
    if visual < 75: blockers.append("主图相似度低于75")
    if pain < 60 and (advantage is None or advantage < 20): blockers.append("痛点不足且成本优势低于20%")
    if trend_retail is None or landed_cost is None: blockers.append("价格缺失，不能通过价格优势门槛")
    return {"match_score": total, "price_advantage_pct": advantage,
        "qualified": total >= threshold and not blockers, "blockers": blockers,
        "components": {"image_similarity": visual, "feature_match": feature,
            "price_advantage": price_score, "pain_resolution": pain, "supplier_quality": supplier}}


def save_sources(root: Path, batch_id: str, platform: str, records: list[dict[str, Any]]) -> int:
    normalized = [normalize_source(platform, raw, i + 1) for i, raw in enumerate(records[:20])]
    with session_scope(root) as session:
        for row in normalized:
            identity = stable_id("TDS", batch_id, platform, row["product_key"])
            existing = session.get(TrendDiscoverySource, identity)
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
            if existing:
                existing.payload, existing.status, existing.page_url = payload, "已采集", row["url"]
            else:
                session.add(TrendDiscoverySource(source_id=identity, batch_id=batch_id, platform=platform,
                    product_key=row["product_key"], page_url=row["url"], status="已采集", payload=payload))
    return len(normalized)


def finalize_batch(root: Path, batch_id: str) -> dict[str, Any]:
    with session_scope(root) as session:
        batch = session.get(TrendDiscoveryBatch, batch_id)
        if not batch: raise ValueError("爆款采集批次不存在")
        filters = json.loads(batch.filters_json)
        records = []
        for source in session.scalars(select(TrendDiscoverySource).where(TrendDiscoverySource.batch_id == batch_id)):
            value = json.loads(source.payload); value["source_id"] = source.source_id; records.append(value)
        candidates = score_sources(records, filters)
        session.execute(delete(TrendDiscoveryCandidate).where(TrendDiscoveryCandidate.batch_id == batch_id))
        for item in candidates:
            trend_id = stable_id("TR", item["candidate_id"]) if item["status"] == "晋级" else None
            session.add(TrendDiscoveryCandidate(candidate_id=stable_id("TDCB", batch_id, item["candidate_id"]),
                batch_id=batch_id, trend_id=trend_id, status=item["status"], score=item["trend_score"],
                evidence_coverage=item["evidence_coverage"], payload=json.dumps(item, ensure_ascii=False, sort_keys=True)))
        result = {"source_count": len(records), "candidate_count": len(candidates),
            "qualified_count": len([x for x in candidates if x["status"] == "晋级"]),
            "candidates": candidates}
        batch.status = "完成" if result["qualified_count"] else "部分完成"
        batch.result_json = json.dumps(result, ensure_ascii=False, sort_keys=True)
        batch.updated_at = now()
    publish_qualified(root, batch_id)
    return result


def publish_qualified(root: Path, batch_id: str) -> int:
    current = read_business(root, "pintan_market_trending_products.csv")
    by_id = {row["trend_id"]: row for row in current}
    covers=[]
    with session_scope(root) as session:
        batch = session.get(TrendDiscoveryBatch, batch_id)
        category = batch.category if batch else ""
        rows = list(session.scalars(select(TrendDiscoveryCandidate).where(
            TrendDiscoveryCandidate.batch_id == batch_id, TrendDiscoveryCandidate.status == "晋级")))
        for row in rows:
            item = json.loads(row.payload)
            sources = item["sources"]
            source_urls = [x["url"] for x in sources]
            details=next((x for x in sources if x.get('specs_text') or x.get('selling_points')),{})
            reviews=[]
            for source in sources:reviews.extend(source.get('review_snippets') or [])
            payload = {
                "trend_id": row.trend_id, "title": item["title"], "category": category,
                "platform": "+".join(PLATFORM_NAMES.get(x, x) for x in item["source_platforms"]),
                "retail_price_cny": "" if item.get("retail_price_cny") is None else str(item["retail_price_cny"]),
                "displayed_sales_range": max((x.get("sales_text", "") for x in sources), key=len, default=""),
                "core_features": (details.get('specs_text') or details.get('selling_points') or item["title"])[:2000],
                "target_audience": "", "use_scenarios": "",
                "negative_review_pain_points": "|".join(reviews[:10]),
                "pain_point_evidence_summary": "；".join(reviews[:10]),
                "search_keyword": category, "source_entry_url": source_urls[0] if source_urls else "",
                "source_entry_urls": "|".join(source_urls), "cover_url": item.get("cover_url", ""),
                "trend_score": str(item["trend_score"]), "evidence_coverage_pct": str(item["evidence_coverage"]),
                "platform_count": str(item["platform_count"]), "data_origin": "browser_visible_evidence",
                "verification_status": "页面证据待复核", "observed_date": now()[:10],
                "field_provenance": json.dumps({"score": "trend-discovery-v1.0", "sources": source_urls}, ensure_ascii=False),
            }
            by_id[row.trend_id] = payload
            product=session.get(Product,row.trend_id)
            if product:
                product.title=item["title"];product.category=category;product.source_product_url=payload["source_entry_url"]
            else:
                session.add(Product(product_id=row.trend_id,title=item["title"],category=category,
                    source_platform=payload["platform"],source_product_url=payload["source_entry_url"],verification_status="页面证据待复核"))
            if item.get("cover_url"):covers.append((row.trend_id,item["cover_url"]))
            signal = session.get(TrendSignal, row.trend_id)
            if signal: signal.payload = json.dumps(payload, ensure_ascii=False)
            else: session.add(TrendSignal(trend_id=row.trend_id, payload=json.dumps(payload, ensure_ascii=False)))
    write_business(root, "pintan_market_trending_products.csv", list(by_id.values()))
    from .workbench import capture_cover
    def download_cover(value):
        product_id,url=value
        try:capture_cover(root,product_id,{"images":[{"region":"主图","url":url}]})
        except Exception:pass
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(download_cover,covers))
    return len(rows)


def batch_view(root: Path, batch_id: str) -> dict[str, Any]:
    with session_scope(root) as session:
        batch = session.get(TrendDiscoveryBatch, batch_id)
        if not batch: raise ValueError("爆款采集批次不存在")
        sources = list(session.scalars(select(TrendDiscoverySource).where(TrendDiscoverySource.batch_id == batch_id)))
        candidates = list(session.scalars(select(TrendDiscoveryCandidate).where(TrendDiscoveryCandidate.batch_id == batch_id)
            .order_by(TrendDiscoveryCandidate.score.desc())))
        candidate_views=[{**json.loads(x.payload), "trend_id": x.trend_id} for x in candidates]
        from .workbench import with_covers
        with_covers(root,candidate_views)
        return {"batch_id": batch.batch_id, "category": batch.category, "status": batch.status,
            "filters": json.loads(batch.filters_json), "progress": json.loads(batch.progress_json),
            "error_type": batch.error_type, "created_at": batch.created_at, "updated_at": batch.updated_at,
            "source_count": len(sources), "candidates": candidate_views}
