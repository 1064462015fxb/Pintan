"""CSV 数据仓库与跨数据集质量检查。"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import TypeVar, Callable

from .models import SupplyProduct, TrendProduct

T = TypeVar("T")


def _read(path: Path, factory: Callable[[dict[str, str]], T], id_attr: str) -> list[T]:
    """读取 UTF-8 CSV、构造领域对象并检查主键唯一性。

    使用 ``utf-8-sig`` 可以同时兼容普通 UTF-8 和带 BOM 的 Excel 导出文件。
    ``factory`` 把通用读取逻辑与具体模型的字段校验解耦。
    """
    if not path.exists():
        raise FileNotFoundError(f"数据文件不存在: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [factory(row) for row in csv.DictReader(handle)]
    seen: set[str] = set()
    for item in rows:
        item_id = str(getattr(item, id_attr))
        if item_id in seen:
            raise ValueError(f"主键重复: {item_id}")
        seen.add(item_id)
    return rows


def load_supplies(path: Path) -> list[SupplyProduct]:
    """加载并验证 1688 供应候选表。"""
    return _read(path, SupplyProduct.from_row, "supply_id")


def load_trends(path: Path) -> list[TrendProduct]:
    """加载并验证淘宝/抖音趋势信号表。"""
    return _read(path, TrendProduct.from_row, "trend_id")


def validate_inputs(supplies: list[SupplyProduct], trends: list[TrendProduct]) -> dict[str, int]:
    """执行需要同时查看两个数据集的业务校验。

    返回的计数可直接用于 CLI 或未来的 API 健康检查。这里只检查输入事实，
    不检查 Agent 结果，因为结果是在流水线后续阶段产生的。
    """
    if not supplies or not trends:
        raise ValueError("两张输入表都必须至少包含一条记录")
    for supply in supplies:
        if supply.wholesale_price_cny <= 0 or supply.moq <= 0:
            raise ValueError(f"{supply.supply_id} 的价格或起订量无效")
        if not 0 <= supply.repurchase_rate_pct <= 100 or not 0 <= supply.supplier_quality_proxy_pct <= 100:
            raise ValueError(f"{supply.supply_id} 的百分比字段超出 0-100")
    for trend in trends:
        if trend.retail_price_cny <= 0 or trend.listed_days < 0:
            raise ValueError(f"{trend.trend_id} 的价格或上架天数无效")
    return {
        "supplies": len(supplies), "trends": len(trends),
        "taobao": sum(x.platform == "淘宝" for x in trends),
        "douyin": sum(x.platform == "抖音" for x in trends),
    }
