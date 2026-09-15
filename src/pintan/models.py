"""领域数据模型与 CSV 字段转换。

本模块位于数据边界层：它只负责把 CSV 中的字符串转换为具有明确类型的
Python 对象，不进行选品评分，也不调用模型。保留 ``raw`` 字段是为了让
Agent 的每条解释都能回溯到最初输入，避免生成式模型补写不存在的事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _float(value: str | float | int | None, name: str) -> float:
    """将 CSV 值转换为浮点数，并提供包含字段名的可读错误。"""
    try:
        return float(value or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是数值，实际为 {value!r}") from exc


def _int(value: str | float | int | None, name: str) -> int:
    """将 CSV 值转换为整数；拒绝 1.5 这类会被静默截断的值。"""
    number = _float(value, name)
    if not number.is_integer():
        raise ValueError(f"{name} 必须是整数，实际为 {value!r}")
    return int(number)


def split_tags(value: str) -> tuple[str, ...]:
    """按数据集约定的 ``|`` 分隔标签，同时移除空白项。"""
    return tuple(x.strip() for x in (value or "").split("|") if x.strip())


@dataclass(frozen=True)
class SupplyProduct:
    """1688 供应侧候选商品。

    这里的字段均来自供应输入表。类被冻结（``frozen=True``），确保后续
    Agent 无法在解释阶段修改价格、规格、销量等事实字段。
    """
    supply_id: str
    title: str
    category: str
    wholesale_price_cny: float
    estimated_shipping_cny: float
    moq: int
    sales_30d: int
    repurchase_rate_pct: float
    supplier_years: int
    supplier_quality_proxy_pct: float
    material: str
    battery_capacity_mah: int
    power_w: float
    feature_tags: tuple[str, ...]
    verification_status: str
    raw: dict[str, str] = field(repr=False)

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "SupplyProduct":
        """从一行 CSV 构造供应商品，并完成基础类型与必填项校验。"""
        required = ("supply_id", "title", "category", "wholesale_price_cny", "feature_tags")
        missing = [key for key in required if not row.get(key)]
        if missing:
            raise ValueError(f"1688 数据缺少必填字段: {', '.join(missing)}")
        return cls(
            supply_id=row["supply_id"], title=row["title"], category=row["category"],
            wholesale_price_cny=_float(row.get("wholesale_price_cny"), "wholesale_price_cny"),
            estimated_shipping_cny=_float(row.get("estimated_shipping_cny"), "estimated_shipping_cny"),
            moq=_int(row.get("moq"), "moq"), sales_30d=_int(row.get("sales_30d"), "sales_30d"),
            repurchase_rate_pct=_float(row.get("repurchase_rate_pct"), "repurchase_rate_pct"),
            supplier_years=_int(row.get("supplier_years"), "supplier_years"),
            supplier_quality_proxy_pct=_float(row.get("supplier_quality_proxy_pct"), "supplier_quality_proxy_pct"),
            material=row.get("material", ""), battery_capacity_mah=_int(row.get("battery_capacity_mah"), "battery_capacity_mah"),
            power_w=_float(row.get("power_w"), "power_w"), feature_tags=split_tags(row["feature_tags"]),
            verification_status=row.get("verification_status", "待复核"), raw=dict(row),
        )

    @property
    def landed_cost(self) -> float:
        """返回演示版到手成本：批发价 + 单件预估运费。

        生产环境还应加入平台佣金、包装、售后和税费；Demo 暂不虚构这些
        未在输入表中提供的成本。
        """
        return self.wholesale_price_cny + self.estimated_shipping_cny


@dataclass(frozen=True)
class TrendProduct:
    """淘宝/抖音需求侧趋势商品。

    淘宝与抖音不可比的互动指标仍保留在 ``raw`` 中，但当前稳定评分只读取
    两个平台共有且已标准化的字段，避免直接比较点赞量和收藏量。
    """
    trend_id: str
    platform: str
    title: str
    category: str
    listed_days: int
    sales_growth_pct: float
    retail_price_cny: float
    core_features: tuple[str, ...]
    negative_review_pain_points: tuple[str, ...]
    pain_point_evidence_summary: str
    search_keyword: str
    verification_status: str
    raw: dict[str, str] = field(repr=False)

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "TrendProduct":
        """从趋势 CSV 构造对象，并限制当前 Demo 支持的平台集合。"""
        required = ("trend_id", "platform", "title", "category", "core_features", "retail_price_cny")
        missing = [key for key in required if not row.get(key)]
        if missing:
            raise ValueError(f"趋势数据缺少必填字段: {', '.join(missing)}")
        if row["platform"] not in {"淘宝", "抖音"}:
            raise ValueError(f"不支持的平台: {row['platform']}")
        return cls(
            trend_id=row["trend_id"], platform=row["platform"], title=row["title"], category=row["category"],
            listed_days=_int(row.get("listed_days"), "listed_days"),
            sales_growth_pct=_float(row.get("sales_growth_pct"), "sales_growth_pct"),
            retail_price_cny=_float(row.get("retail_price_cny"), "retail_price_cny"),
            core_features=split_tags(row["core_features"]),
            negative_review_pain_points=split_tags(row.get("negative_review_pain_points", "")),
            pain_point_evidence_summary=row.get("pain_point_evidence_summary", ""), search_keyword=row.get("search_keyword", ""),
            verification_status=row.get("verification_status", "待复核"), raw=dict(row),
        )


@dataclass
class SelectionResult:
    """规则引擎与 Agent 共同形成的最终结果。

    数值字段由规则引擎一次性计算；Agent 只允许写入解释、痛点和风险字段。
    该边界是系统可复现、可审计的关键。
    """
    run_id: str
    trend_id: str
    supply_id: str
    rank_within_trend: int
    feature_similarity_score: float
    price_advantage_score: float
    pain_point_resolution_score: float
    supplier_quality_score: float
    heat_score: float
    competition_score: float
    margin_score: float
    gap_score: float
    trust_score: float
    total_score: float
    match_score: float
    recommendation: str
    recommendation_reason: str = ""
    resolved_pain_points: list[str] = field(default_factory=list)
    unresolved_pain_points: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    evidence_fields: list[str] = field(default_factory=list)
    agent_status: str = "rules_only"
    model: str = "rules-v1"
    prompt_version: str = "product-selection-v1.0"
    calculated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为 CSV 可写字典，将列表编码为可逆的 ``|`` 分隔文本。"""
        data = vars(self).copy()
        for key in ("resolved_pain_points", "unresolved_pain_points", "risk_flags", "evidence_fields"):
            data[key] = "|".join(data[key])
        return data
