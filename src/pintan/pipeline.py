"""端到端选品流水线：加载 -> 评分 -> Agent 解释 -> 导出。"""

from __future__ import annotations

import csv
import json
import logging
import uuid
from pathlib import Path

from .agent import apply_explanation, build_agent, explain
from .models import SelectionResult
from .repository import load_supplies, load_trends, validate_inputs
from .scoring import rank_candidates

LOGGER = logging.getLogger("pintan")


def run_pipeline(
    root: Path,
    trend_ids: set[str] | None,
    top_k: int,
    use_agent: bool = True,
) -> tuple[list[SelectionResult], list[dict]]:
    """运行一次选品任务。

    Agent 初始化或单条解释失败都不会丢失规则结果：失败项会写入可读的规则
    解释并标记 ``agent_status=fallback``，使比赛现场在断网时仍可演示。
    """
    # 两张 CSV 是唯一事实输入；历史结果表不参与新一轮计算。
    supplies = load_supplies(root / "pintan_1688_supply_candidates.csv")
    trends = load_trends(root / "pintan_market_trending_products.csv")
    validate_inputs(supplies, trends)
    if trend_ids:
        unknown = trend_ids - {x.trend_id for x in trends}
        if unknown:
            raise ValueError(f"未知 trend_id: {', '.join(sorted(unknown))}")
        trends = [x for x in trends if x.trend_id in trend_ids]
    # run_id 用于区分执行批次，不参与评分，因此不会影响排序复现性。
    run_id = f"RUN-{uuid.uuid4().hex[:12].upper()}"
    rows = [item for trend in trends for item in rank_candidates(run_id, trend, supplies, top_k)]
    supply_by_id = {x.supply_id: x for x in supplies}
    trend_by_id = {x.trend_id: x for x in trends}
    explanations: list[dict] = []

    # 只创建一次 Agent 并复用于本批次，避免为每个候选重复加载 Skill。
    agent = None
    if use_agent:
        try:
            agent = build_agent(root)
        except Exception as exc:
            LOGGER.warning("Agent unavailable; status=fallback error_type=%s", type(exc).__name__)

    for result in rows:
        trend, supply = trend_by_id[result.trend_id], supply_by_id[result.supply_id]
        if agent is None:
            # 主动关闭 Agent、缺少 Key 或依赖不可用时走统一降级路径。
            result.recommendation_reason = f"规则匹配：{trend.search_keyword} -> {supply.title}；匹配分 {result.match_score}"
            result.agent_status = "fallback"
        else:
            try:
                explanation, elapsed = explain(agent, trend, supply, result)
                allowed = json.dumps({"trend": trend.raw, "supply": supply.raw}, ensure_ascii=False)
                apply_explanation(result, explanation, allowed)
                LOGGER.info("Agent explanation completed model=%s elapsed=%.2fs", result.model, elapsed)
                explanations.append({"trend_id": result.trend_id, "supply_id": result.supply_id, **explanation})
            except Exception as exc:
                # 单条失败不应中断整个批次；错误日志不包含 API Key 或请求头。
                LOGGER.warning("Agent explanation failed trend=%s supply=%s error_type=%s", result.trend_id, result.supply_id, type(exc).__name__)
                result.recommendation_reason = f"规则匹配：{trend.search_keyword} -> {supply.title}；匹配分 {result.match_score}"
                result.agent_status = "fallback"
    return rows, explanations


def write_outputs(root: Path, rows: list[SelectionResult], explanations: list[dict]) -> tuple[Path, Path]:
    """输出 Excel 友好的 UTF-8 BOM CSV 和模型解释 JSON。

    CSV 始终包含规则结果；JSON 只收录成功通过安全门的 Agent 原始解释，
    因而可用于区分“模型成功”和“规则降级”两类运行状态。
    """
    output_dir = root / "outputs"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path, json_path = output_dir / "selection_results.csv", output_dir / "agent_explanations.json"
    dictionaries = [row.to_dict() for row in rows]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(dictionaries[0]))
        writer.writeheader()
        writer.writerows(dictionaries)
    json_path.write_text(json.dumps(explanations, ensure_ascii=False, indent=2), encoding="utf-8")
    return csv_path, json_path
