"""两个互相隔离的 Deep Agent 适配器。

稳定 Agent 必须调用确定性计算工具；爆款 Agent 必须独立判断十个候选。任何
模型异常都只记录异常类型，不打印请求、响应或密钥。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .data_store import SEARCH_FILE, TREND_FILE, atomic_write_csv, atomic_write_json, read_csv
from .model_config import load_model_config
from .stable import deterministic_report, score_stable_products
from .demo_data import MATCH_FIELDS

LOGGER = logging.getLogger("pintan")
TREND_PROMPT_VERSION = "trending-match-v2.0"


def _imports():
    try:
        from deepagents import create_deep_agent
        from deepagents.backends import FilesystemBackend
        from langchain_core.tools import tool
        from langchain_openai import ChatOpenAI
        return create_deep_agent, FilesystemBackend, tool, ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("未安装 Deep Agents 依赖") from exc


def _text(response: dict[str, Any]) -> str:
    for message in reversed(response.get("messages", [])):
        # 新版 LangChain 的 AI 消息可能使用内容块列表；不能跳过最终答案
        # 后误把前一个 read_file/计算工具的内容当成模型输出。
        if getattr(message, "type", None) != "ai":
            continue
        value = getattr(message, "content", None)
        if isinstance(value, str) and value.strip(): return value.strip()
        if isinstance(value, list):
            parts = [block.get("text", "") for block in value if isinstance(block, dict) and block.get("type") == "text"]
            answer = "\n".join(part for part in parts if isinstance(part, str)).strip()
            if answer: return answer
    raise ValueError("模型未返回文本")


def _json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"): text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    first_obj, first_arr = text.find("{"), text.find("[")
    start = first_arr if first_obj < 0 else first_obj if first_arr < 0 else min(first_obj, first_arr)
    if start < 0: raise ValueError("模型响应不含 JSON")
    # raw_decode 在首个完整JSON处停止，避免模型尾随说明或第二段示例破坏解析。
    return json.JSONDecoder().raw_decode(text[start:])[0]


def _make_agent(root: Path, role: str, system: str, tools: list[Any] | None = None):
    create, Backend, _, ChatOpenAI = _imports()
    config = load_model_config("trending" if role in {"content","collector","enrichment"} else role)  # type: ignore[arg-type]
    model = ChatOpenAI(api_key=config.api_key, base_url=config.base_url, model=config.model,
        temperature=0, timeout=90, max_retries=0, max_tokens=6000, streaming=True,
        extra_body={"enable_thinking": os.getenv("PINTAN_ENABLE_THINKING", "false").lower() == "true"}
        if config.model.startswith("qwen") else None)
    # Skill 源必须是包含技能子目录的父目录。每次运行复制到独立目录，
    # 防止两个 Agent 读到彼此 Skill，更不能访问项目中的 .env。
    skill = {"stable":"stable-selection","trending":"trending-selection","content":"content-master","collector":"data-collector","enrichment":"data-enrichment"}[role]
    runtime = root / "outputs/agent_runtime" / uuid.uuid4().hex
    destination = runtime / "skills" / skill
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "skills" / skill / "SKILL.md", destination / "SKILL.md")
    backend = Backend(root_dir=str(runtime.resolve()), virtual_mode=True)
    system += f" 必须先使用 read_file 读取 /skills/{skill}/SKILL.md，再执行任务。"
    middleware = []
    if role in {"trending","content","collector","enrichment"}:
        from langchain.agents.middleware import wrap_model_call

        @wrap_model_call
        def bounded_matching_tools(request, handler):
            """本流程只需读 Skill 后作答，不允许无关委派或反复写文件。"""
            try:
                _skill_trace({"messages": request.messages}, skill)
                read_complete = True
            except ValueError:
                read_complete = False
            allowed = [] if read_complete else [t for t in request.tools
                if (t.get("name") if isinstance(t, dict) else t.name) == "read_file"]
            choice = "none" if read_complete else {"type": "function", "function": {"name": "read_file"}}
            return handler(request.override(tools=allowed, tool_choice=choice))

        middleware.append(bounded_matching_tools)
    return create(model=model, system_prompt=system, tools=tools or [], backend=backend,
        skills=["/skills/"], middleware=middleware,
        name=f"pintan-{role}-agent"), config.model


def _skill_trace(response: dict[str, Any], expected: str) -> dict[str, Any]:
    """仅记录技能名及工具名，不持久化请求头、密钥或完整对话。"""
    calls = [c for m in response.get("messages", []) for c in getattr(m, "tool_calls", [])]
    read_ids = {c.get("id") for c in calls if c.get("name") == "read_file" and f"/{expected}/SKILL.md" in str(c.get("args", {}))}
    # create_deep_agent 的公开输出只含 messages，不包含 middleware 内部状态。
    # 以对应 read_file 的成功 ToolMessage 为凭据，而不是误读缺失的 metadata。
    read = any(getattr(m, "tool_call_id", None) in read_ids
               and getattr(m, "status", "success") != "error"
               and expected in str(getattr(m, "content", ""))
               for m in response.get("messages", []))
    if not read:
        raise ValueError("Agent 未完成 Skill 加载和读取")
    return {"skills_loaded": [expected], "skill_read": read, "tools_called": [c["name"] for c in calls]}


def run_stable_agent(root: Path, use_model: bool = True) -> dict[str, Any]:
    """运行稳定工具并生成汇报；模型不可提交或覆盖任何评分。"""
    if not use_model:
        rows = score_stable_products(root)
        return {"status": "rules_only", "model": "rules", "report": deterministic_report(rows), "top10": rows[:10]}
    try:
        _, _, tool, _ = _imports()
        tool_called = False

        @tool
        def calculate_stable_scores() -> str:
            """评分候选并按利润、市场验证、经营适配三维规则晋级稳定池。"""
            nonlocal tool_called
            tool_called = True
            rows = score_stable_products(root)
            return json.dumps({"count": len(rows), "top10": rows[:10],
                "score_version": rows[0]["score_version"] if rows else 'stable-rules-v5-three-pillars'}, ensure_ascii=False)

        agent, model = _make_agent(root, "stable", "你是稳定选品汇报 Agent。必须先调用 calculate_stable_scores；只基于工具结果用中文汇报，不得修改分数。明确区分页面证据、店铺代理指标、AI建议售价和规则兜底价。", [calculate_stable_scores])
        started = time.monotonic()
        response = agent.invoke({"messages": [{"role": "user", "content": "请运行稳定评分工具并汇报前十结果、前三名优势和数据限制。"}]}, config={"recursion_limit": 12})
        if not tool_called: raise ValueError("稳定 Agent 未调用计算工具")
        trace = _skill_trace(response, "stable-selection")
        rows = read_csv(root / "pintan_stable_products.csv")
        rows.sort(key=lambda r: int(r.get("stable_rank") or 9999))
        result = {"status": "success", "model": model, "elapsed_seconds": round(time.monotonic()-started, 2), "report": _text(response), "top10": rows[:10]}
        result.update(trace)
    except Exception as exc:
        LOGGER.warning("stable agent fallback error_type=%s", type(exc).__name__)
        rows = score_stable_products(root)
        result = {"status": "fallback", "model": "rules", "report": deterministic_report(rows), "top10": rows[:10]}
    atomic_write_json(root / "outputs/stable_latest.json", result)
    return result


class CandidateAssessment(BaseModel):
    search_id: str
    feature_match_score: float = Field(ge=0, le=100)
    price_advantage_score: float = Field(ge=0, le=100)
    pain_resolution_score: float = Field(ge=0, le=100)
    supplier_quality_score: float = Field(ge=0, le=100)
    match_score: float = Field(ge=0, le=100)
    recommendation: str
    recommendation_reason: str
    resolved_pain_points: list[str]
    unresolved_pain_points: list[str]
    risk_flags: list[str]
    evidence_fields: list[str]

    @model_validator(mode="after")
    def arithmetic(self):
        expected = round(self.price_advantage_score*.4 + self.feature_match_score*.25 + self.pain_resolution_score*.25 + self.supplier_quality_score*.1, 1)
        if abs(self.match_score - expected) > .11: raise ValueError(f"match_score 应为 {expected}")
        return self


def _validate_assessments(value: Any, trend: dict[str, str], candidates: list[dict[str, str]]) -> list[CandidateAssessment]:
    if isinstance(value, dict): value = value.get("assessments")
    if not isinstance(value, list): raise ValueError("assessments 必须是数组")
    items = []
    errors = []
    pains = set(filter(None, trend.get("negative_review_pain_points", "").split("|")))
    allowed = {f"trend.{k}" for k in trend} | {f"search.{k}" for k in candidates[0]} if candidates else set()
    for record in value:
        if isinstance(record, dict):
            identity = record.get("search_id", "unknown")
            for field in ("resolved_pain_points", "unresolved_pain_points"):
                if isinstance(record.get(field), list):
                    extra = [x for x in record[field] if not isinstance(x, str) or x not in pains]
                    if extra:
                        errors.append(f"{identity}.{field} 必须逐字选自 {sorted(pains)}，不允许改写；错误项：{extra}")
            refs = record.get("evidence_fields", [])
            if isinstance(refs, list) and (not refs or any(not isinstance(x, str) or x not in allowed for x in refs)):
                errors.append(f"{identity}.evidence_fields 必须引用输入中的 trend.字段 或 search.字段")
        try:
            items.append(CandidateAssessment.model_validate(record))
        except ValueError as exc:
            # 一次返回全部候选的错误，避免仅有的一次纠正修完首行又败在下一行。
            identity = record.get("search_id", "unknown") if isinstance(record, dict) else "unknown"
            details = exc.errors() if hasattr(exc, "errors") else [{"msg": str(exc)}]
            errors.append(f"{identity}: " + "; ".join(x["msg"] for x in details))
    if errors:
        raise ValueError(" | ".join(errors))
    expected = {x["search_id"] for x in candidates}
    if len(items) != 10 or {x.search_id for x in items} != expected: raise ValueError("必须完整评估指定的 10 个 search_id")
    for item in items:
        if not item.evidence_fields or any(x not in allowed for x in item.evidence_fields): raise ValueError(f"{item.search_id} 证据字段无效")
        if not set(item.resolved_pain_points + item.unresolved_pain_points) <= pains: raise ValueError(f"{item.search_id} 编造了痛点")
    return items


def _legacy_run_trending_agent(root: Path, trend_id: str) -> dict[str, Any]:
    trends, searches = read_csv(root / TREND_FILE), read_csv(root / SEARCH_FILE)
    trend = next((x for x in trends if x["trend_id"] == trend_id), None)
    if not trend: raise ValueError(f"未知 trend_id: {trend_id}")
    # 历史评分不能进入新一轮输入，避免模型照抄旧分数。
    candidates = [{k: v for k, v in x.items() if k not in MATCH_FIELDS} for x in searches if x["trend_id"] == trend_id]
    if len(candidates) != 10 or len({x["search_id"] for x in candidates}) != 10: raise ValueError("该爆款必须恰好关联10条不重复候选")
    system = "你是爆款供应匹配 Agent。必须阅读 trending-selection Skill，自行对全部十条候选评分。只输出 JSON，不得调用旧规则评分，不得虚构事实。"
    agent, model = _make_agent(root, "trending", system)
    schema = CandidateAssessment.model_json_schema()
    exact_pains = list(filter(None, trend.get("negative_review_pain_points", "").split("|")))
    if exact_pains:
        for field in ("resolved_pain_points", "unresolved_pain_points"):
            schema["properties"][field]["items"] = {"type": "string", "enum": exact_pains}
    payload = {"trend": trend, "candidates": candidates,
        "required_output": {"assessments": "十个对象，严格遵循以下 schema"},
        "allowed_pain_points_verbatim": exact_pains,
        "assessment_schema": schema}
    prompt = "评估以下一个爆款和恰好十个候选。功能标签只能表示可能对应痛点，不能证明实际功效。痛点数组只能逐字复制 allowed_pain_points_verbatim 中的原文，不允许改写或添加任何字。\n" + json.dumps(payload, ensure_ascii=False)
    started = time.monotonic(); last_error = ""
    messages = [{"role": "user", "content": prompt}]
    for attempt in range(2):
        response = agent.invoke({"messages": messages}, config={"recursion_limit": 12})
        try:
            trace = _skill_trace(response, "trending-selection")
            assessments = _validate_assessments(_json(_text(response)), trend, candidates); break
        except Exception as exc:
            last_error = str(exc)
            messages = response["messages"] + [{"role": "user", "content":
                f"上次 JSON 校验失败：{last_error}。保留上次候选和四个分项，不要重新评估。仅修正列出的错误（特别是加权总分），重新输出完整十条 JSON，不省略任何字段。"}]
    else: raise ValueError(f"模型两次输出均未通过校验: {last_error}")
    by_id = {x.search_id: x for x in assessments}; stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ranked = sorted(assessments, key=lambda x: (-x.match_score, x.search_id))
    ranks = {x.search_id: i for i, x in enumerate(ranked, 1)}
    for row in searches:
        if row["trend_id"] != trend_id: continue
        item = by_id[row["search_id"]]
        row.update({"feature_match_score": item.feature_match_score, "price_advantage_score": item.price_advantage_score,
            "pain_resolution_score": item.pain_resolution_score, "supplier_quality_score": item.supplier_quality_score,
            "match_score": item.match_score, "match_rank": ranks[item.search_id], "recommendation": item.recommendation,
            "recommendation_reason": item.recommendation_reason, "resolved_pain_points": "|".join(item.resolved_pain_points),
            "unresolved_pain_points": "|".join(item.unresolved_pain_points), "risk_flags": "|".join(item.risk_flags),
            "evidence_fields": "|".join(item.evidence_fields), "agent_status": "success", "model": model,
            "prompt_version": TREND_PROMPT_VERSION, "matched_at": stamp})
    atomic_write_csv(root / SEARCH_FILE, searches)
    result = {"status": "success", "trend": trend, "model": model, "elapsed_seconds": round(time.monotonic()-started, 2),
        "top3": [next(r for r in searches if r["search_id"] == x.search_id) for x in ranked[:3]], "all10": [next(r for r in searches if r["search_id"] == x.search_id) for x in ranked]}
    result.update(trace)
    atomic_write_json(root / f"outputs/trending_{trend_id}_latest.json", result)
    atomic_write_json(root / f"outputs/trending_{trend_id}_{stamp.replace(':','-')}.json", {"input": payload, "result": result, "prompt_version": TREND_PROMPT_VERSION})
    return result


def run_trending_agent(root: Path, trend_id: str, force: bool = False) -> dict[str, Any]:
    """兼容公开入口；v3 由模型评估分项，本地计算总分并支持缓存。"""
    from .trending import run_trending_agent as run
    return run(root, trend_id, force=force)
