"""1688关键词批量选品：查询建议、候选过滤和批次只读视图。"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field
from sqlalchemy import select

from .database import BatchSelection, BatchSelectionItem, BusinessRow, CollectorTask, session_scope

PLAN_VERSION = "batch-plan-v1"
ACTIVE = {"search_queued", "opening_search", "reading_search", "waiting_user", "collecting", "estimating"}
TERMINAL = {"completed", "partial", "failed", "cancelled", "interrupted"}


class FilterAdvice(BaseModel):
    normalized_keyword: str = Field(min_length=1, max_length=80)
    related_keywords: list[str] = Field(default_factory=list, max_length=8)
    price_min: float | None = Field(default=None, ge=0)
    price_max: float | None = Field(default=None, gt=0)
    max_moq: int | None = Field(default=None, ge=1)
    min_sales: int | None = Field(default=None, ge=0)
    require_dropship: bool = False
    require_free_shipping: bool = False
    min_supplier_years: int | None = Field(default=None, ge=0)
    min_return_rate_pct: float | None = Field(default=None, ge=0, le=100)
    exclude_ads: bool = True
    exclude_keywords: list[str] = Field(default_factory=list, max_length=20)
    rationale: str = Field(default="", max_length=1000)


def default_advice(keyword: str) -> dict[str, Any]:
    raw = re.sub(r"\s+", "", str(keyword or ""))[:80]
    normalized = "家居收纳" if raw in {"家居归纳", "家居收纳"} else raw
    if not normalized:
        raise ValueError("请输入经营方向")
    related = ["桌面收纳", "衣柜收纳", "厨房收纳", "浴室收纳"] if normalized == "家居收纳" else []
    return FilterAdvice(
        normalized_keyword=normalized,
        related_keywords=related,
        max_moq=2,
        exclude_ads=True,
        exclude_keywords=["教程", "加盟", "二手"],
        rationale="优先低起订、可验证价格和有效销量的相关商品；供应商指标在详情页二次核验。",
    ).model_dump()


def recommend_filters(root, keyword: str) -> dict[str, Any]:
    """让文本模型给出受限过滤建议；失败时返回明确标记的确定性建议。"""
    fallback = default_advice(keyword)
    try:
        from langchain_openai import ChatOpenAI
        from .agents import _json
        from .model_config import load_model_config

        config = load_model_config("trending")
        prompt = (
            "你是1688家居日用品批量选品查询规划器。纠正明显错词并给出保守、可验证的过滤条件。"
            "不得声称已经搜索网页；不要设置过窄条件；只返回JSON。字段必须严格匹配："
            '{"normalized_keyword":"","related_keywords":[],"price_min":null,"price_max":null,'
            '"max_moq":null,"min_sales":null,"require_dropship":false,"require_free_shipping":false,'
            '"min_supplier_years":null,"min_return_rate_pct":null,"exclude_ads":true,'
            '"exclude_keywords":[],"rationale":""}。原始经营方向：' + str(keyword)[:80]
        )
        model = ChatOpenAI(api_key=config.api_key, base_url=config.base_url, model=config.model,
                           temperature=0, timeout=90, max_retries=0,
                           extra_body={"enable_thinking": False})
        value = FilterAdvice.model_validate(_json(model.invoke(prompt).content)).model_dump()
        if value["price_min"] is not None and value["price_max"] is not None and value["price_min"] > value["price_max"]:
            raise ValueError("价格区间倒置")
        # 原始经营方向属于用户输入血缘，不能依赖模型回显，也不能被纠词结果覆盖。
        return {"original_keyword": str(keyword)[:80], "source": "ai", "model": config.model,
                "version": PLAN_VERSION, **value}
    except Exception as exc:
        return {"original_keyword": str(keyword)[:80], "source": "rules-fallback",
                "error_type": type(exc).__name__, "version": PLAN_VERSION, **fallback}


def validate_filters(value: dict[str, Any]) -> dict[str, Any]:
    allowed = set(FilterAdvice.model_fields) - {"normalized_keyword", "related_keywords", "rationale"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("包含不支持的过滤条件：" + "、".join(sorted(unknown)))
    base = FilterAdvice(normalized_keyword="临时", **value).model_dump()
    result = {key: base[key] for key in allowed}
    if result["price_min"] is not None and result["price_max"] is not None and result["price_min"] > result["price_max"]:
        raise ValueError("最低价不能高于最高价")
    return result


def candidate_offer(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.hostname != "detail.1688.com":
        return None
    match = re.fullmatch(r"/offer/(\d+)\.html", parsed.path)
    return match.group(1) if match else None


def filter_candidates(candidates: list[dict[str, Any]], filters: dict[str, Any], limit: int = 60) -> list[dict[str, Any]]:
    """只在搜索卡片证据足够时应用过滤；详情级条件留到详情采集后判断。"""
    result, seen = [], set()
    excludes = [str(x).lower() for x in filters.get("exclude_keywords", []) if str(x).strip()]
    for index, item in enumerate(candidates[:200], 1):
        offer = candidate_offer(str(item.get("page_url", "")))
        title = re.sub(r"\s+", " ", str(item.get("title", ""))).strip()[:300]
        if not offer or offer in seen or not title or any(word in title.lower() for word in excludes):
            continue
        if filters.get("exclude_ads") and bool(item.get("is_ad")):
            continue
        price = item.get("price")
        try:
            price = float(price) if price not in (None, "") else None
        except (TypeError, ValueError):
            price = None
        if price is not None and filters.get("price_min") is not None and price < filters["price_min"]:
            continue
        if price is not None and filters.get("price_max") is not None and price > filters["price_max"]:
            continue
        seen.add(offer)
        result.append({"offer_id": offer, "title": title,
                       "page_url": f"https://detail.1688.com/offer/{offer}.html",
                       "relevance_rank": int(item.get("relevance_rank") or index),
                       "card": {k: item.get(k) for k in ("price", "sales_text", "moq_text", "is_ad")}})
        if len(result) >= limit:
            break
    return result


def detail_filter(root, run_id: str, filters: dict[str, Any]) -> tuple[bool, str]:
    """对详情页条件做二次过滤。

    批量预筛的首要目标是形成可评分候选池。页面未展示或采集器暂未识别的字段
    属于“未知”，不能等同于明确不达标；只有存在可核验证据且确实越界时才剔除。
    缺失项会在后续评分中显示为待补充/代理输入。
    """
    from .collector_mapping import read_mapping
    from .database import CollectionRun
    from .scoring_evidence import extract_signals
    with session_scope(root) as session:
        run=session.get(CollectionRun,run_id)
        mapping=read_mapping(session,run_id)
        snapshot=json.loads(run.raw_snapshot_json)
    fields={x['field_name']:x['value'] for x in mapping.get('review_fields',[]) if x.get('included')}
    pairs=mapping.get('spec_price_pairs',[]);prices=[float(x['price_cny']) for x in pairs if x.get('price_cny') is not None]
    text='\n'.join(str(x.get('text','')) for x in snapshot.get('text_blocks',[]))+'\n'+snapshot.get('visible_text','')
    signals=extract_signals(text);numeric=mapping.get('numeric',{})
    if filters.get('price_min') is not None and prices and min(prices)<filters['price_min']:return False,'采购价低于过滤下限'
    if filters.get('price_max') is not None and prices and min(prices)>filters['price_max']:return False,'采购价高于过滤上限'
    if filters.get('max_moq') is not None:
        match=re.search(r'(\d+)',fields.get('minimum_order_text',''))
        if match and int(match.group(1))>filters['max_moq']:return False,'起订量超过上限'
    if filters.get('min_sales') is not None:
        sales=numeric.get('sales_30d') or signals.get('sales_proxy',{}).get('value')
        if sales is not None and float(sales)<filters['min_sales']:return False,'销量低于下限'
    if filters.get('require_dropship') and re.search(r'不支持(?:一件)?代发|不可代发',text):return False,'页面明确不支持一件代发'
    if filters.get('require_free_shipping') and re.search(r'不包邮|运费到付|买家承担运费',text):return False,'页面明确不包邮'
    if filters.get('min_supplier_years') is not None:
        years=numeric.get('supplier_years')
        if years is not None and float(years)<filters['min_supplier_years']:return False,'经营年限低于下限'
    if filters.get('min_return_rate_pct') is not None:
        rate=signals.get('repurchase_proxy',{}).get('value') or numeric.get('repurchase_rate_pct')
        if rate is not None and float(rate)<filters['min_return_rate_pct']:return False,'回头率低于下限'
    return True,''


def batch_view(root, batch_id: str) -> dict[str, Any]:
    with session_scope(root) as session:
        batch = session.get(BatchSelection, batch_id)
        if not batch:
            raise ValueError("批量任务不存在")
        payload = json.loads(batch.payload)
        items = list(session.scalars(select(BatchSelectionItem).where(
            BatchSelectionItem.batch_id == batch_id).order_by(BatchSelectionItem.relevance_rank)))
        item_views=[]
        for item in items:
            values=json.loads(item.payload)
            run_id=values.get('run_id')
            if not run_id and item.collector_task_id:
                task=session.get(CollectorTask,item.collector_task_id)
                if task:
                    run_id=json.loads(task.payload).get('run_id')
            scoring_row=(session.get(BusinessRow,('candidate',item.product_id)) or
                         session.get(BusinessRow,('stable',item.product_id))) if item.product_id else None
            scoring=json.loads(scoring_row.payload) if scoring_row else {}
            display_status=item.status
            if item.status in {'collected','estimating','scored','partial_score','needs_input'} and scoring_row:
                display_status=('scored' if scoring_row.kind=='stable' else
                                ('rejected' if scoring.get('promotion_status')=='rejected' else 'needs_input'))
            item_views.append({"offer_id":item.offer_id,"title":item.title,"page_url":item.page_url,
                "relevance_rank":item.relevance_rank,"status":display_status,
                "collector_task_id":item.collector_task_id,"product_id":item.product_id,
                "run_id":run_id,"error_type":item.error_type,
                "promotion_status":scoring.get('promotion_status'),"total_score":scoring.get('total_score',''),
                "profit_score":scoring.get('profit_score',''),"market_validation_score":scoring.get('market_validation_score',''),
                "operating_fit_score":scoring.get('operating_fit_score',''),
                "missing_score_fields":scoring.get('missing_score_fields',''),**values})
        return {
            "batch_id": batch.batch_id, "original_keyword": batch.original_keyword,
            "normalized_keyword": batch.normalized_keyword, "filters": json.loads(batch.filters_json),
            "target_count": batch.target_count, "max_candidates": batch.max_candidates,
            "status": batch.status, "phase": payload.get("phase", batch.status),
            "message": payload.get("message", ""), "found": payload.get("found", 0),
            "collected": sum(x['status'] in {"collected", "estimating", "scored", "partial_score", "needs_input"} for x in item_views),
            "scored": sum(x['status'] == "scored" for x in item_views),
            "failed": sum(x['status'] in {"failed", "partial_score", "needs_input"} for x in item_views),
            "created_at": batch.created_at, "updated_at": batch.updated_at,
            "items":item_views,
        }
