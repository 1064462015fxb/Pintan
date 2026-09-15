"""内容母版 Agent：逐商品生成，事实可追溯，历史版本不可覆盖。"""
from __future__ import annotations
import hashlib,json,time,uuid
from pathlib import Path
from typing import Any
from pydantic import BaseModel,Field,model_validator
from sqlalchemy import select
from .agents import _json,_make_agent,_skill_trace,_text
from .database import ContentEvidence,ContentMaster,ContentVersion,product_bundle,session_scope,utcnow
PROMPT_VERSION="content-master-v1.0"
class Point(BaseModel):
    text:str=Field(min_length=1,max_length=160); claim_ids:list[str]; support_level:str
class MasterPayload(BaseModel):
    positioning:dict[str,Any]; selling_points:list[Point]=Field(min_length=1); pain_points:dict[str,Any]
    article:dict[str,Any]; video:dict[str,Any]; image_requirements:list[dict[str,Any]]
    forbidden_expressions:list[str]; missing_evidence:list[str]; data_limitations:list[str]
    @model_validator(mode="after")
    def images(self):
        if any(x.get("status")!="待采集" for x in self.image_requirements): raise ValueError("图片状态必须为待采集")
        return self
def validate_payload(raw,bundle):
    value=MasterPayload.model_validate(raw); allowed={x["claim_id"] for x in bundle["claims"]}; used=set()
    for p in value.selling_points:
        if p.support_level=="事实支持" and not p.claim_ids: raise ValueError("事实卖点缺少证据")
        if any(x not in allowed for x in p.claim_ids): raise ValueError("卖点引用不存在的事实")
        used.update(p.claim_ids)
    for image in value.image_requirements:
        ids=image.get("required_claim_ids",[])
        if any(x not in allowed for x in ids): raise ValueError("图片需求引用不存在的事实")
        used.update(ids)
    return value.model_dump(),used
def get_master(root:Path,mid:str):
    with session_scope(root) as s:
        m=s.get(ContentMaster,mid)
        if not m: raise ValueError("母版不存在")
        vs=list(s.scalars(select(ContentVersion).where(ContentVersion.master_id==mid).order_by(ContentVersion.version.desc())))
        return {"master_id":m.master_id,"product_id":m.product_id,"trend_id":m.trend_id,"status":m.status,"current_version":m.current_version,"created_at":m.created_at,"versions":[{"version":v.version,"status":v.status,"content":json.loads(v.content_json),"model":v.model,"prompt_version":v.prompt_version,"created_at":v.created_at} for v in vs]}
def list_masters(root):
    with session_scope(root) as s: ids=list(s.scalars(select(ContentMaster.master_id).order_by(ContentMaster.created_at.desc())))
    return [get_master(root,x) for x in ids]
def run_content_master(root:Path,product_id:str,trend_id:str|None=None,force=False):
    bundle=product_bundle(root,product_id,trend_id); skill=(root/"skills/content-master/SKILL.md").read_text(encoding="utf-8")
    agent,model=_make_agent(root,"content","你是内容母版 Agent。只生成平台无关内容，严格区分事实、可能性和创意。")
    key=hashlib.sha256(json.dumps({"bundle":bundle,"model":model,"skill":skill,"prompt":PROMPT_VERSION},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    with session_scope(root) as s:
        old=s.scalar(select(ContentMaster).where(ContentMaster.cache_key==key,ContentMaster.status=="success").order_by(ContentMaster.created_at.desc()))
        if old and not force:return get_master(root,old.master_id)|{"cache_hit":True}
    prompt={"task":"为单个商品生成图文与短视频内容母版","input":bundle,"rules":{"images":"未采集，只生成需求","output":"strict_json"}}
    started=time.monotonic(); messages=[{"role":"user","content":json.dumps(prompt,ensure_ascii=False)}]
    response=agent.invoke({"messages":messages},config={"recursion_limit":12})
    trace=_skill_trace(response,"content-master")
    try: content,used=validate_payload(_json(_text(response)),bundle)
    except (ValueError,TypeError,json.JSONDecodeError) as error:
        # 只允许一次结构纠正；不把校验错误转化为虚构内容。
        messages=response["messages"]+[{"role":"user","content":f"输出未通过校验：{type(error).__name__}。请依据同一输入重新返回完整严格JSON；不得增加事实。"}]
        response=agent.invoke({"messages":messages},config={"recursion_limit":12})
        trace=_skill_trace(response,"content-master"); content,used=validate_payload(_json(_text(response)),bundle)
    mid="CM-"+uuid.uuid4().hex[:16]; now=utcnow()
    with session_scope(root) as s:
        s.add(ContentMaster(master_id=mid,product_id=product_id,trend_id=trend_id,cache_key=key,status="success",current_version=1,created_at=now)); s.flush()
        v=ContentVersion(master_id=mid,version=1,content_json=json.dumps(content,ensure_ascii=False),status="草稿",model=model,prompt_version=PROMPT_VERSION,created_at=now); s.add(v); s.flush()
        for cid in used:s.add(ContentEvidence(version_id=v.id,node_path="generated",claim_id=cid))
    return get_master(root,mid)|{"cache_hit":False,"elapsed_seconds":round(time.monotonic()-started,2),**trace}
def save_version(root,mid,content):
    current=get_master(root,mid); payload,used=validate_payload(content,product_bundle(root,current["product_id"],current["trend_id"]))
    with session_scope(root) as s:
        m=s.get(ContentMaster,mid); n=m.current_version+1; m.current_version=n
        v=ContentVersion(master_id=mid,version=n,content_json=json.dumps(payload,ensure_ascii=False),status="草稿",model="human-edit",prompt_version=PROMPT_VERSION,created_at=utcnow()); s.add(v); s.flush()
        for cid in used:s.add(ContentEvidence(version_id=v.id,node_path="edited",claim_id=cid))
    return get_master(root,mid)
def confirm_version(root,mid,number):
    with session_scope(root) as s:
        v=s.scalar(select(ContentVersion).where(ContentVersion.master_id==mid,ContentVersion.version==number))
        if not v:raise ValueError("母版版本不存在")
        v.status="已确认"
    return get_master(root,mid)
