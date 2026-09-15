"""采集字段的 AI 辅助补全。

页面采集事实永远优先。本模块只为少量可从现有文字合理推导的字段生成
待复核草稿，不生成不可验证的交易、供应商或精确参数数据。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .agents import _json, _make_agent, _skill_trace, _text
from .asset_records import metadata,update_metadata
from .collector_mapping import ALIASES, CATALOG, detect_review_anomalies, read_mapping, save_review
from .database import CollectionRun, ProductAsset, session_scope,utcnow
from .media_pipeline import _ocr
from sqlalchemy import select

PROMPT_VERSION = "collector-enrichment-v1.1"

# 这些字段可以在有文字线索时给出建议；交易和供应链事实不能靠常识补造。
INFERABLE_FIELDS = {
    "title", "category", "material", "product_features",
}
FORBIDDEN_FIELDS = set(CATALOG) - INFERABLE_FIELDS

OCR_PRIORITY={"信息图":0,"参数图":1,"SKU图":2,"详情图":3,"主图":4,"功能图":5,"场景图":6,"待分类商品图":7}


def matched_ocr_fields(text:str,target_fields:set[str])->set[str]:
    """只判断OCR中是否出现目标字段标签，不凭图片常识猜测字段已经找到。"""
    compact="".join(str(text or "").split())
    matched=set()
    for label,field in ALIASES.items():
        if field in target_fields and label in compact:matched.add(field)
    return matched


def collect_ocr_evidence(root:Path,product_id:str,target_fields:set[str]|None=None,limit:int=16)->list[dict[str,Any]]:
    """逐张OCR；目标字段均命中后立刻停止，避免发送无关文字给模型。"""
    target_fields=set(target_fields or INFERABLE_FIELDS)
    with session_scope(root) as s:
        assets=[a for a in s.scalars(select(ProductAsset).where(ProductAsset.product_id==product_id))
                if a.local_path and not a.status.startswith("已合并:") and a.status!="复核排除"]
        assets.sort(key=lambda a:(OCR_PRIORITY.get(a.asset_type,99),-((a.width or 0)*(a.height or 0)),a.asset_id))
        work=[{"asset_id":a.asset_id,"asset_type":a.asset_type,"local_path":a.local_path,
               "ocr_text":a.ocr_text or "","attempted":bool(metadata(s,a.asset_id).get("ocr_status"))} for a in assets[:limit]]
    result=[];fallback=None;covered=set()
    for item in work:
        text=item["ocr_text"]
        if not text and not item["attempted"]:
            try:
                path=(root/item["local_path"]).resolve();path.relative_to(root.resolve())
                data=path.read_bytes();text=_ocr(data) if len(data)<=10*1024*1024 else ""
            except (OSError,ValueError):text=""
            with session_scope(root) as s:
                asset=s.get(ProductAsset,item["asset_id"])
                if asset:asset.ocr_text=text or None
                update_metadata(s,item["asset_id"],ocr_status="识别成功" if text else "未识别到文字",
                                ocr_attempted_at=utcnow(),ocr_verification_status="待复核")
        if text:
            matched=matched_ocr_fields(text,target_fields)
            evidence={"evidence_id":"OCR:"+item["asset_id"],"asset_id":item["asset_id"],
                "asset_type":item["asset_type"],"text":text[:1200],"matched_fields":sorted(matched),
                "verification_status":"待复核"}
            if matched:result.append(evidence);covered.update(matched)
            elif fallback is None:fallback=evidence
            if target_fields and target_fields.issubset(covered):break
    # 没有任何明确标签时，只提供第一张有文字的图片作为弱上下文；不会把后续
    # 无关OCR全文一并发给模型。部分字段未命中时也最多补一份弱上下文。
    if fallback and (not result or not target_fields.issubset(covered)):result.append(fallback)
    return result


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    field_name: str
    value: str = Field(min_length=1, max_length=200)
    confidence: float = Field(ge=0, le=.85)
    rationale: str = Field(min_length=1, max_length=300)
    based_on_field_ids: list[str] = Field(min_length=1, max_length=10)
    risk_note: str = Field(min_length=1, max_length=200)


class SuggestionSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=20)


def validate_suggestions(raw: Any, missing: set[str], evidence_ids: set[str]) -> list[dict[str, Any]]:
    """约束模型结果：仅缺失、安全字段，且引用真实存在的复核字段ID。"""
    value=SuggestionSet.model_validate(raw)
    result=[];seen=set()
    for item in value.suggestions:
        if item.field_name not in CATALOG:raise ValueError("模型返回未知字段")
        if item.field_name in FORBIDDEN_FIELDS:raise ValueError("模型尝试推测不可验证字段")
        if item.field_name not in missing:raise ValueError("模型尝试覆盖已采集字段")
        if item.field_name in seen:raise ValueError("模型返回重复补全字段")
        if not set(item.based_on_field_ids).issubset(evidence_ids):raise ValueError("模型引用了不存在的字段证据")
        seen.add(item.field_name);result.append(item.model_dump())
    return result


def run_collection_enrichment(root: Path, run_id: str) -> dict[str, Any]:
    """调用隔离 Deep Agent，验证后把建议写入人工复核草稿。"""
    with session_scope(root) as s:
        run=s.get(CollectionRun,run_id)
        if not run:raise ValueError("采集记录不存在")
        if run.status=="已人工复核":raise ValueError("已确认记录不能补全，请重新采集生成新版本")
        mapping=read_mapping(s,run_id)
        if not mapping:raise ValueError("采集映射不存在")
        current=mapping["review_fields"];product_id=run.product_id
    anomalies=detect_review_anomalies(current);bad_ids={x["review_id"] for x in anomalies}
    included=[x for x in current if x.get("included") and x.get("review_id") not in bad_ids]
    present={x["field_name"] for x in included if str(x.get("value") or "").strip()}
    missing=(INFERABLE_FIELDS-present)|{x["field_name"] for x in anomalies if x["field_name"] in INFERABLE_FIELDS}
    if not missing:return {"status":"nothing_to_complete","added":0,"prompt_version":PROMPT_VERSION}
    ocr_evidence=collect_ocr_evidence(root,product_id,missing) if product_id else []
    evidence_ids={str(x["review_id"]) for x in current}|{x["evidence_id"] for x in ocr_evidence}
    if not evidence_ids:raise ValueError("没有可用于推断的已采集字段")
    captured=[{"field_id":x["review_id"],"field_name":x["field_name"],"value":x["value"],
               "source_kind":x.get("source_kind","页面采集")} for x in included]
    agent,model=_make_agent(root,"enrichment",
        "你是商品数据补全 Agent。页面事实优先，只能补充允许字段并输出严格JSON；所有输出都是AI推测草稿，不能声称来自1688页面。")
    prompt={"task":"根据已有采集字段，建议补全可合理推导的缺失字段",
            "captured_fields":captured,
            "suspected_errors":anomalies,
            "ocr_evidence":ocr_evidence,
            "missing_fields":[{"field_name":x,"label":CATALOG[x][0],"reason":CATALOG[x][2]} for x in sorted(missing)],
            "forbidden_fields":sorted(FORBIDDEN_FIELDS),"prompt_version":PROMPT_VERSION}
    prompt["output_schema"]=SuggestionSet.model_json_schema()
    messages=[{"role":"user","content":json.dumps(prompt,ensure_ascii=False)}]
    last_error=None;response=None;suggestions=None
    for attempt in range(2):
        response=agent.invoke({"messages":messages},config={"recursion_limit":12})
        try:
            _skill_trace(response,"data-enrichment")
            suggestions=validate_suggestions(_json(_text(response)),missing,evidence_ids)
            break
        except (ValueError,TypeError,json.JSONDecodeError) as exc:
            last_error=exc
            if attempt:
                raise ValueError("AI补全返回格式或事实引用不符合约束，自动纠正一次后仍失败") from exc
            # 保留首次对话上下文，让模型只纠正结构；不向日志或数据库保存原始响应。
            messages=[*response.get("messages",[]),{"role":"user","content":
                "上次输出未通过后端校验。请严格按 output_schema 只返回一个JSON对象；"
                "不要使用Markdown，不要增加字段，不要覆盖已有字段，不要推测forbidden_fields。"}]
    if suggestions is None:raise ValueError("AI补全未返回可验证结果") from last_error
    trace=_skill_trace(response,"data-enrichment")
    additions=[{"field_name":x["field_name"],"value":x["value"],"included":True,
                "source_kind":"AI推测","confidence":x["confidence"],"rationale":x["rationale"],
                "based_on_field_ids":x["based_on_field_ids"],"risk_note":x["risk_note"]} for x in suggestions]
    # 高确定性错位字段默认取消采用，但原始值与证据完整保留，用户可恢复。
    reviewed=[{**x,"included":False,"risk_note":next((a["reason"] for a in anomalies if a["review_id"]==x.get("review_id")),x.get("risk_note",""))}
              if x.get("review_id") in bad_ids else x for x in current]
    with session_scope(root) as s:
        saved=save_review(s,run_id,reviewed+additions)
    return {"status":"completed","added":len(additions),"model":model,"prompt_version":PROMPT_VERSION,
            "skills_loaded":trace["skills_loaded"],"fields":[x["field_name"] for x in additions],
            "anomalies_excluded":len(anomalies),"ocr_evidence_count":len(ocr_evidence),"review_count":len(saved)}
