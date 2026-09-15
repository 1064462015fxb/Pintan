"""确定性选品评分引擎。

模型不参与本模块的任何数值计算。相同 CSV、相同版本代码和相同 ``top_k``
必然产生相同分数与排序；Deep Agent 只在其后解释结果。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from .models import SelectionResult, SupplyProduct, TrendProduct


# 将评论痛点中的自然语言关键词映射到可验证的供应特征。
# 这是显式、可审计的 Demo 词典；无法命中的痛点一律视为“未解决”，
# 而不是让模型凭常识推断商品能力。
PAIN_FEATURE_MAP = {
    "续航": {"大容量", "充电宝功能", "电量数显", "数显"},
    "电量": {"电量数显", "数显"},
    "噪声": {"低噪", "睡眠定时"},
    "夹持": {"大夹力", "夹持", "可弯曲", "360度旋转"},
    "清洁": {"前罩可拆洗"},
    "积灰": {"前罩可拆洗"},
    "重量": {"轻量", "超薄", "迷你"},
    "安装": {"磁吸", "壁挂台式两用", "夹持"},
    "角度": {"多角度", "360度旋转", "出风口可调", "自动摇头"},
    "色差": {"定制色"},
    "漏水": {"水箱可视"},
    "支架": {"三脚架"},
    "太阳能": {"太阳能充电"},
}


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    """把所有评分限制在 0-100，防止异常输入造成越界。"""
    return min(high, max(low, value))


def round1(value: float) -> float:
    """统一保留一位小数；微小偏移用于规避二进制浮点临界误差。"""
    return round(value + 1e-10, 1)


def _feature_similarity(trend: TrendProduct, supply: SupplyProduct) -> float:
    """计算需求特征与供应标签的规则相似度。

    完全一致标签权重最高，包含关系按半个匹配计分，搜索词命中标题只提供
    小额奖励。35 分基础分代表“同属已筛选类目”，并不等于已推荐。
    """
    trend_tags, supply_tags = set(trend.core_features), set(supply.feature_tags)
    exact = len(trend_tags & supply_tags)
    partial = sum(
        1 for trend_tag in trend_tags for supply_tag in supply_tags
        if trend_tag not in supply_tags and (trend_tag in supply_tag or supply_tag in trend_tag)
    )
    keyword_bonus = 1 if any(token and token in supply.title for token in trend.search_keyword.replace("风扇", "").split()) else 0
    denominator = max(1, len(trend_tags))
    return round1(clamp(35 + 60 * min(1, (exact + partial * 0.5) / denominator) + keyword_bonus * 5))


def _resolved_pains(trend: TrendProduct, supply: SupplyProduct) -> tuple[list[str], list[str]]:
    """依据显式词典把趋势痛点分成已解决和未解决两组。"""
    features = set(supply.feature_tags)
    resolved, unresolved = [], []
    for pain in trend.negative_review_pain_points:
        candidates: set[str] = set()
        for keyword, mapped in PAIN_FEATURE_MAP.items():
            if keyword in pain:
                candidates |= mapped
        if candidates and any(any(feature in actual or actual in feature for actual in features) for feature in candidates):
            resolved.append(pain)
        else:
            unresolved.append(pain)
    return resolved, unresolved


def score_pair(run_id: str, trend: TrendProduct, supply: SupplyProduct) -> SelectionResult:
    """计算一个趋势商品与一个供应商品的两套评分。

    ``match_score`` 衡量“是否适合作为该爆款的供应链平替”；
    ``total_score`` 衡量“供应商品自身是否值得经营”。两者必须并列展示，
    不能合并成无法解释的单一分数。
    """
    feature_similarity = _feature_similarity(trend, supply)

    # 价格优势以趋势零售价减去供应到手成本计算。乘数用于把毛利率映射到
    # 0-100 的展示尺度，不代表真实利润承诺。
    gross_margin_pct = clamp((trend.retail_price_cny - supply.landed_cost) / trend.retail_price_cny * 100)
    price_advantage = round1(clamp(gross_margin_pct * 1.7))
    resolved, unresolved = _resolved_pains(trend, supply)
    pain_resolution = round1(45 + 55 * len(resolved) / max(1, len(trend.negative_review_pain_points)))
    supplier_quality = round1(clamp(supply.supplier_quality_proxy_pct))

    # 五维稳定选品分。成交量取对数，避免头部销量把其他维度完全淹没。
    heat = round1(clamp(18 * math.log10(max(1, supply.sales_30d)) + supply.repurchase_rate_pct * 0.8))
    competition = round1(clamp(45 + feature_similarity * 0.35 - min(25, supply.sales_30d / 400)))
    margin = round1(clamp(gross_margin_pct * 1.75))
    gap = round1(feature_similarity * 0.55 + pain_resolution * 0.45)
    trust = supplier_quality
    # 稳定选品权重：热度25% + 竞争20% + 利润25% + 差异化20% + 信任10%。
    total = round1(heat * 0.25 + competition * 0.20 + margin * 0.25 + gap * 0.20 + trust * 0.10)
    # 爆款平替权重：价格40% + 特征25% + 痛点25% + 供应商10%。
    match = round1(price_advantage * 0.40 + feature_similarity * 0.25 + pain_resolution * 0.25 + supplier_quality * 0.10)
    recommendation = "强推" if match >= 82 and total >= 72 else "测试" if match >= 65 and total >= 58 else "放弃"
    risks = list(unresolved)
    if supply.verification_status != "已核验" or trend.verification_status != "已核验":
        risks.append("输入数据待复核")
    return SelectionResult(
        run_id=run_id, trend_id=trend.trend_id, supply_id=supply.supply_id, rank_within_trend=0,
        feature_similarity_score=feature_similarity, price_advantage_score=price_advantage,
        pain_point_resolution_score=pain_resolution, supplier_quality_score=supplier_quality,
        heat_score=heat, competition_score=competition, margin_score=margin, gap_score=gap,
        trust_score=trust, total_score=total, match_score=match, recommendation=recommendation,
        resolved_pain_points=resolved, unresolved_pain_points=unresolved, risk_flags=risks,
        evidence_fields=["trend.core_features", "trend.negative_review_pain_points", "supply.feature_tags", "supply.wholesale_price_cny", "supply.estimated_shipping_cny"],
        calculated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def rank_candidates(run_id: str, trend: TrendProduct, supplies: list[SupplyProduct], top_k: int = 2) -> list[SelectionResult]:
    """为单个趋势商品评分全部供应候选并返回前 ``top_k`` 条。

    排序依次使用匹配分、稳定总分、供应 ID。最后的 ID 排序保证同分情况下
    仍有稳定结果，不依赖 Python 容器或 CSV 读取顺序。
    """
    if top_k < 1:
        raise ValueError("top_k 必须大于 0")
    ranked = sorted(
        (score_pair(run_id, trend, supply) for supply in supplies),
        key=lambda x: (-x.match_score, -x.total_score, x.supply_id),
    )[:top_k]
    for index, result in enumerate(ranked, 1):
        result.rank_within_trend = index
    return ranked
