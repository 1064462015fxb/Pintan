"""Deep Agents 适配层与模型输出安全门。

本模块是唯一直接接触模型的地方。API Key 只从进程环境变量读取；模型输出
必须经过 JSON、风险词和证据字段校验后，才能写回 ``SelectionResult``。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .models import SelectionResult, SupplyProduct, TrendProduct
from .model_config import load_model_config


SYSTEM_PROMPT = """你是品探选品解释 Agent。数值评分已经由确定性规则引擎完成，你不得修改任何分数、排名或推荐等级。
必须读取适用的 product-selection Skill，并严格区分输入事实与模型推断。最终只返回一个 JSON 对象，不要 Markdown。"""


def _last_text(result: dict[str, Any]) -> str:
    """兼容 LangChain 消息对象和普通字典，提取最后一条有效文本。"""
    for message in reversed(result.get("messages", [])):
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"].strip()
    raise ValueError("Agent 没有返回文本")


def _extract_json(text: str) -> dict[str, Any]:
    """从模型文本中提取 JSON 对象，并容忍常见的 Markdown 代码围栏。"""
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[1].rsplit("```", 1)[0]
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Agent 返回内容中没有 JSON 对象")
    value = json.loads(stripped[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("Agent JSON 必须是对象")
    return value


def build_agent(project_root: Path):
    """创建加载项目 Skill 的 Deep Agent。

    ``FilesystemBackend`` 使用 ``virtual_mode=True``，将文件工具限制在项目根
    目录内；这里没有使用 ``LocalShellBackend``，因此不会向 Agent 开放宿主机
    命令执行能力。
    """
    config = load_model_config("stable")
    try:
        from deepagents import create_deep_agent
        from deepagents.backends import FilesystemBackend
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("未安装 deepagents/langchain-openai，请先安装项目依赖") from exc
    # Token Plan 提供 OpenAI 兼容接口，URL 使用纯文本而非 Markdown 链接。
    # temperature=0 只能降低解释波动；真正的数值复现由规则引擎保证。
    model = ChatOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        model=config.model,
        temperature=0,
        max_retries=1,
        timeout=60,
    )
    backend = FilesystemBackend(root_dir=str(project_root.resolve()), virtual_mode=True)
    return create_deep_agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        backend=backend,
        skills=["/skills/"],
        name="pintan-selection-agent",
    )


def explain(
    agent: Any,
    trend: TrendProduct,
    supply: SupplyProduct,
    result: SelectionResult,
) -> tuple[dict[str, Any], float]:
    """向 Agent 提供三段隔离数据并返回结构化解释与耗时。

    ``locked_rule_result`` 明确告知模型哪些值不可修改；输入只包含当前候选，
    避免模型从其他商品记录中错误借用规格。
    """
    payload = {
        "trend_facts": {
            "trend_id": trend.trend_id, "platform": trend.platform, "title": trend.title,
            "core_features": trend.core_features, "negative_review_pain_points": trend.negative_review_pain_points,
            "pain_point_evidence_summary": trend.pain_point_evidence_summary,
        },
        "supply_facts": {
            "supply_id": supply.supply_id, "title": supply.title, "material": supply.material,
            "battery_capacity_mah": supply.battery_capacity_mah, "power_w": supply.power_w,
            "feature_tags": supply.feature_tags, "wholesale_price_cny": supply.wholesale_price_cny,
            "estimated_shipping_cny": supply.estimated_shipping_cny,
        },
        "locked_rule_result": {
            "rank": result.rank_within_trend, "match_score": result.match_score,
            "total_score": result.total_score, "recommendation": result.recommendation,
            "resolved_pain_points": result.resolved_pain_points,
            "unresolved_pain_points": result.unresolved_pain_points,
        },
        "required_output": {
            "recommendation_reason": "string",
            "resolved_pain_points": ["string"], "unresolved_pain_points": ["string"],
            "risk_flags": ["string"], "evidence_fields": ["trend.xxx or supply.xxx"],
        },
    }
    started = time.monotonic()
    response = agent.invoke({"messages": [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]})
    return _extract_json(_last_text(response)), time.monotonic() - started


def apply_explanation(result: SelectionResult, explanation: dict[str, Any], allowed_values: str) -> None:
    """校验并合并 Agent 解释，但永不覆盖数值评分字段。

    ``allowed_values`` 是本次趋势和供应原始行的序列化文本。高风险声明只有
    在原始输入确实出现时才允许保留；证据字段必须带 ``trend.`` 或
    ``supply.`` 前缀，以便前端或审计日志定位来源。
    """
    reason = str(explanation.get("recommendation_reason", "")).strip()
    if not reason:
        raise ValueError("Agent 缺少 recommendation_reason")
    forbidden_claims = ["认证", "第一", "100%", "医用", "治疗", "永久"]
    if any(word in reason and word not in allowed_values for word in forbidden_claims):
        raise ValueError("Agent 解释包含无依据的高风险声明")
    evidence = [str(x) for x in explanation.get("evidence_fields", [])]
    if not evidence or any(not (x.startswith("trend.") or x.startswith("supply.")) for x in evidence):
        raise ValueError("Agent 解释缺少有效证据字段")
    result.recommendation_reason = reason
    result.resolved_pain_points = [str(x) for x in explanation.get("resolved_pain_points", result.resolved_pain_points)]
    result.unresolved_pain_points = [str(x) for x in explanation.get("unresolved_pain_points", result.unresolved_pain_points)]
    result.risk_flags = [str(x) for x in explanation.get("risk_flags", result.risk_flags)]
    result.evidence_fields = evidence
    result.agent_status = "success"
    result.model = load_model_config("stable", require_key=False).model
