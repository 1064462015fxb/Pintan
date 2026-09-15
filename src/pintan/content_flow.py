"""面向用户的内容流程：风格诊断、隐藏母版、平台适配和确定性复核。"""
from __future__ import annotations
import hashlib,json,uuid
from pathlib import Path
from pydantic import BaseModel,Field
from sqlalchemy import select
from .agents import _json,_make_agent,_skill_trace,_text
from .content_master import get_master,run_content_master
from .database import AngleDiagnosis,ContentChoice,ContentMaster,PlatformContent,PlatformContentVersion,PlatformRule,product_bundle,session_scope,utcnow

ANGLES={"痛点解决型","使用场景型","参数展示型","性价比型","人群种草型","差异化功能型","供应链信任型","对比测评型"}
PLATFORMS={"抖音","淘宝","京东","Amazon","小红书"}
REQUIRED={
 "抖音":["商品标题","商品卡卖点","短视频脚本","口播","字幕","分镜","直播话术"],
 "淘宝":["商品标题","短标题","核心卖点","主图文案","详情页模块","属性建议"],
 "京东":["商品标题","卖点","规格参数","详情页模块","问答建议"],
 "小红书":["笔记标题","正文","首图文案","配图顺序","话题","互动引导"],
 "Amazon":["Product Title","Bullet Points","Product Description","Search Terms","A+ Content","Image Requirements"]}
RULES={
 "Amazon":{"version":"amazon-us-2026.07","source_type":"官方公开规则","source_url":"https://sellercentral.amazon.com/seller-forums/discussions/t/33f0a42a-17f1-46ef-b110-ba7512a3c881","rules":["美国站英语","非媒体类标题不超过75字符","不得编造品牌、认证或参数","英制和公制并列"]},
 "淘宝":{"version":"project-taobao-1.0","source_type":"项目自定义规则","source_url":None,"rules":["内容如实且字段一致","禁止绝对化与无证据功效","发布前到平台后台复核"]},
 "抖音":{"version":"project-douyin-1.0","source_type":"项目自定义规则","source_url":None,"rules":["口播与商品事实一致","禁止虚假销量认证和功效","发布前到平台后台复核"]},
 "京东":{"version":"project-jd-1.0","source_type":"项目自定义规则","source_url":None,"rules":["标题属性与详情一致","禁止无证据承诺","发布前到平台后台复核"]},
 "小红书":{"version":"project-xhs-1.0","source_type":"项目自定义规则","source_url":None,"rules":["种草体验不得冒充实测","禁止虚假功效和绝对化表达","发布前到平台后台复核"]}}
# 发布工作台字段组织是项目模板，不冒充店铺后台的实时类目校验结果。
LISTING_PROFILES={
 '淘宝':{'workbench':'千牛卖家中心','source_url':'https://developer.alibaba.com/docs/doc.htm?articleId=108956&docType=1&treeId=1','source_type':'官方商品发布字段参考 + 项目图文模板','verified_at':'2026-09-12','instructions':'标题突出商品名称与有证据的特点；详情页依次组织商品介绍、功能、规格、使用说明。属性建议只填已确认事实。主图文案简短，不照搬供应商促销。'},
 '京东':{'workbench':'京麦工作台','source_url':'https://h5.jd.com/article/901.html','source_type':'官方帮助入口参考；具体模板为项目自定义，待店铺后台复核','verified_at':None,'instructions':'组织标题、卖点、规格参数表、详情介绍与问答。规格逐项对应已确认资料，主图与详情图保持同款同规格。'}
}
DIAGNOSIS_VERSION="angle-diagnosis-v1.0"; PLATFORM_VERSION="platform-content-v1.1"
class Angle(BaseModel):
    type:str; confidence:float=Field(ge=0,le=1); reason:str; evidence_claim_ids:list[str]
class Diagnosis(BaseModel):
    primary:Angle; alternatives:list[Angle]=Field(min_length=2,max_length=2); not_recommended:list[dict]; material_gaps:list[str]
def _hash(x):return hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def _call(root,prompt):
    agent,model=_make_agent(root,"content","你是品探内容策略与平台适配 Agent。必须基于事实ID，输出严格JSON，不得编造事实或平台规则。")
    response=agent.invoke({"messages":[{"role":"user","content":json.dumps(prompt,ensure_ascii=False)}]},config={"recursion_limit":12}); _skill_trace(response,"content-master")
    return _json(_text(response)),model
def diagnose_angle(root:Path,product_id:str,trend_id:str|None=None,force=False):
    bundle=product_bundle(root,product_id,trend_id); key=_hash({"bundle":bundle,"version":DIAGNOSIS_VERSION})
    with session_scope(root) as s:
        old=s.scalar(select(AngleDiagnosis).where(AngleDiagnosis.cache_key==key))
        if old and not force:return public_diagnosis(old)|{"cache_hit":True}
    allowed={x['claim_id'] for x in bundle['claims']}
    prompt={"mode":"diagnose_angle","allowed_angles":sorted(ANGLES),"input":bundle,
            "allowed_claim_ids":sorted(allowed),"required":"一个primary、恰好两个alternatives、not_recommended、material_gaps；primary及alternatives每项必须有type、confidence、reason、evidence_claim_ids。ID必须从allowed_claim_ids逐字复制，禁止EV来源ID、字段名或自行编号；证据不足可用空数组并注明。confidence为0到1"}
    raw,model=_call(root,prompt)
    refs=lambda data:[cid for x in [data.get('primary',{}),*data.get('alternatives',[])] for cid in x.get('evidence_claim_ids',x.get('evidence_ids',x.get('claim_ids',[])))]
    if any(cid not in allowed for cid in refs(raw)):
        raw,model=_call(root,{**prompt,'correction':'上次引用了不存在的事实ID，请仅复制allowed_claim_ids中的完整ID。保留证据门禁，不要创造替代ID。','previous_output':raw})
    # 兼容模型常见的等价键名，再由严格模型完成最终验证。
    for item in [raw.get("primary",{}),*raw.get("alternatives",[])]:
        if "type" not in item and "angle" in item:item["type"]=item.pop("angle")
        # Qwen 有时会用 reasoning/rationale 表示“推荐理由”。这些键与
        # reason 语义等价，只做字段名归一化，不改写模型内容。
        if "reason" not in item:
            item["reason"] = item.pop("reasoning", item.pop("rationale", ""))
        if "evidence_claim_ids" not in item:
            item["evidence_claim_ids"]=item.pop("evidence_ids",item.pop("claim_ids",[]))
    raw["material_gaps"]=[x.get("gap_description",x.get("description","")) if isinstance(x,dict) else x for x in raw.get("material_gaps",[])]
    raw['not_recommended']=[{'type':x,'reason':'模型未提供详细理由'} if isinstance(x,str) else x for x in raw.get('not_recommended',[])]
    value=Diagnosis.model_validate(raw); entries=[value.primary,*value.alternatives]
    if any(x.type not in ANGLES for x in entries):raise ValueError("模型返回未定义风格")
    allowed={x["claim_id"] for x in bundle["claims"]}
    if any(cid not in allowed for x in entries for cid in x.evidence_claim_ids):raise ValueError("风格诊断引用不存在的事实")
    did="DG-"+uuid.uuid4().hex[:16]
    with session_scope(root) as s:s.add(AngleDiagnosis(diagnosis_id=did,product_id=product_id,trend_id=trend_id,cache_key=key,result_json=json.dumps(value.model_dump(),ensure_ascii=False),model=model,prompt_version=DIAGNOSIS_VERSION,created_at=utcnow()))
    with session_scope(root) as s:return public_diagnosis(s.get(AngleDiagnosis,did))|{"cache_hit":False}
def public_diagnosis(d):return {"diagnosis_id":d.diagnosis_id,"product_id":d.product_id,"trend_id":d.trend_id,"recommendation":json.loads(d.result_json),"model":d.model,"created_at":d.created_at}
def save_choice(root,did,angles,custom=""):
    with session_scope(root) as s:
        d=s.get(AngleDiagnosis,did)
        if not d:raise ValueError("风格诊断不存在")
        if not angles and not custom.strip():raise ValueError("至少选择一种风格")
        if any(x not in ANGLES for x in angles):raise ValueError("包含未定义风格")
        cid="CH-"+uuid.uuid4().hex[:16]; s.add(ContentChoice(choice_id=cid,diagnosis_id=did,selected_json=json.dumps(angles,ensure_ascii=False),custom_angle=custom.strip(),created_at=utcnow()))
    return {"choice_id":cid,"selected_angles":angles,"custom_angle":custom.strip()}
def scan(fields,bundle,platform):
    text=json.dumps(fields,ensure_ascii=False); issues=[]
    # “第一张/第一部分”只是图片或详情页顺序，不属于排名承诺；只拦截具有
    # 营销排名含义的组合，避免模型反复删除正常的版式说明仍无法通过。
    for word in ["全网第一","行业第一","销量第一","排名第一","第一品牌","第一选择","最强","绝对","100%","永久","零噪音"]:
        if word in text:issues.append({"severity":"high","rule":"绝对化或无证据表述","text":word})
    evidence=' '.join(c.get('value','') for c in bundle.get('claims',[]) if 'AI' not in c.get('verification_status',''))
    # 材质和形态不能证明防漏、承重、粘贴损伤或使用寿命。
    for word in ['防漏','防止液体渗漏','不伤内饰','不留胶','不倾倒','防倒','稳固','食品级','抗菌','承重','呕吐']:
        if word in text and word not in evidence:issues.append({'severity':'high','rule':'缺少实证的性能或用途承诺','text':word})
    if platform=="Amazon" and len(str(fields.get("Product Title","")))>75:issues.append({"severity":"high","rule":"Amazon标题超过75字符","text":"Product Title"})
    return issues

def remove_unsupported_claims(value,issues):
    """三轮模型改写仍失败时，仅删除已命中的风险短语，绝不补造替代性能。"""
    words=sorted({str(x.get('text') or '') for x in issues if x.get('text')},key=len,reverse=True)
    def clean(item):
        if isinstance(item,dict):return {k:clean(v) for k,v in item.items()}
        if isinstance(item,list):return [clean(v) for v in item]
        if not isinstance(item,str):return item
        text=item
        for word in words:text=text.replace(word,'')
        text=text.replace('，，','，').replace('、、','、').replace('。。','。').strip(' ，、；。')
        return text or '请以商品已确认属性和实际页面信息为准'
    return clean(value)
def generate_platform(root:Path,did:str,choice_id:str,platform:str,force=False):
    if platform not in PLATFORMS:raise ValueError("不支持的平台")
    with session_scope(root) as s:
        d=s.get(AngleDiagnosis,did); choice=s.get(ContentChoice,choice_id)
        if not d or not choice or choice.diagnosis_id!=did:raise ValueError("诊断或用户选择无效")
        product_id,trend_id=d.product_id,d.trend_id; chosen=json.loads(choice.selected_json); custom=choice.custom_angle
    bundle=product_bundle(root,product_id,trend_id); rule=RULES[platform]
    # 母版自身按当前事实计算缓存，不能盲目复用最近一版旧事实母版。
    master=run_content_master(root,product_id,trend_id)
    full_internal=master["versions"][0]["content"]
    # 平台调用只取策划核心，降低长JSON截断概率；完整母版仍保留数据库。
    internal={k:full_internal.get(k) for k in ("positioning","selling_points","pain_points","forbidden_expressions","missing_evidence","data_limitations")}
    profile=LISTING_PROFILES.get(platform,{})
    key=_hash({"master":internal,"facts":bundle,"profile":profile,"chosen":chosen,"custom":custom,"platform":platform,"rule":rule,"version":PLATFORM_VERSION})
    with session_scope(root) as s:
        old=s.scalar(select(PlatformContent).where(PlatformContent.cache_key==key).order_by(PlatformContent.created_at.desc()))
        if old and not force:return get_platform_content(root,old.content_id)|{"cache_hit":True}
    before=scan(internal,bundle,platform)
    raw,model=_call(root,{"mode":"generate_platform_content","platform":platform,"market":"US" if platform=="Amazon" else "中国","selected_angles":chosen,"custom_angle":custom,"internal_master":internal,"verified_input":bundle,"listing_profile":profile,"fact_policy":"仅当前claims支持确定性商品表述；旧母版与当前事实冲突以当前事实为准。SKU采购价不是店铺零售价，不得直接生成消费者价格承诺。缺少品牌、认证、库存、物流承诺时不填造。评分是内部参考，不可作为广告卖点。","rules":rule,"required_fields":REQUIRED[platform],"precheck_issues":before,"output":{"fields":"对象，键必须与required_fields完全一致；每字段简洁，列表最多5项","changes":"简短改写记录数组；不得在字符串中使用未转义引号"}})
    fields=raw.get("fields") or raw.get("platform_content") or raw.get("content") or {k:raw[k] for k in REQUIRED[platform] if k in raw}; changes=raw.get("changes",[])
    # 允许模型给字段名附加长度说明，但输出给前端时统一回契约名称。
    if isinstance(fields,dict) and set(fields)!=set(REQUIRED[platform]):
        normalized={}
        for expected in REQUIRED[platform]:
            hit=next((v for k,v in fields.items() if k.strip().lower()==expected.lower() or k.strip().lower().startswith(expected.lower()+" ") or k.startswith(expected+"（")),None)
            if hit is not None:normalized[expected]=hit
        fields=normalized
    if set(fields)!=set(REQUIRED[platform]):raise ValueError("平台内容字段不完整: "+",".join(map(str,fields.keys()))[:240])
    issues=scan(fields,bundle,platform)
    for correction_round in range(3):
        if not issues:break
        revised,model=_call(root,{'mode':'correct_platform_content','fields':fields,'issues':issues,'input':bundle,
            'required_fields':REQUIRED[platform],
            'instruction':'逐条删除issues中命中的风险短语，不要用同义词或另一种性能承诺替换；仅据当前商品标题与已确认事实改写。版式顺序可以写第1张/第2张。可自立不等于稳固不倒，材质不证明防漏。只返回fields和changes，fields键保持不变。',
            'correction_round':correction_round+1})
        candidate=revised.get('fields',{})
        if set(candidate)!=set(REQUIRED[platform]):
            changes.append('第%d轮合规改写字段不完整，保留上一轮并继续安全处理'%(correction_round+1))
            continue
        fields=candidate;changes=changes+revised.get('changes',[]);issues=scan(fields,bundle,platform)
    if issues:
        removed=sorted({x['text'] for x in issues})
        fields=remove_unsupported_claims(fields,issues)
        changes.append('合规兜底仅删除未获证据支持的风险短语：'+'、'.join(removed))
        issues=scan(fields,bundle,platform)
        if issues:raise ValueError('平台文案合规处理未通过：'+'、'.join(sorted({x['text'] for x in issues})))
    risk="低"
    cid="PC-"+uuid.uuid4().hex[:16]; now=utcnow()
    with session_scope(root) as s:
        existing=s.get(PlatformRule,platform+":"+rule["version"])
        if not existing:s.add(PlatformRule(rule_id=platform+":"+rule["version"],platform=platform,version=rule["version"],source_type=rule["source_type"],source_url=rule["source_url"],verified_at="2026-09-09",rules_json=json.dumps(rule["rules"],ensure_ascii=False)))
        s.add(PlatformContent(content_id=cid,master_id=master["master_id"],choice_id=choice_id,platform=platform,market="US" if platform=="Amazon" else "CN",cache_key=key,current_version=1,risk_level=risk,created_at=now))
        s.add(PlatformContentVersion(content_id=cid,version=1,visible_json=json.dumps(fields,ensure_ascii=False),issues_json=json.dumps(issues,ensure_ascii=False),changes_json=json.dumps(changes,ensure_ascii=False),status="草稿",model=model,created_at=now))
    return get_platform_content(root,cid)|{"cache_hit":False}
def get_platform_content(root,cid):
    with session_scope(root) as s:
        c=s.get(PlatformContent,cid)
        if not c:raise ValueError("平台内容不存在")
        v=s.scalar(select(PlatformContentVersion).where(PlatformContentVersion.content_id==cid,PlatformContentVersion.version==c.current_version)); rule=RULES[c.platform]
        return {"listing_profile":LISTING_PROFILES.get(c.platform,{}),"publication_checklist":["确认目标类目及店铺必填属性","逐SKU确认零售价与库存，采购价不等于零售价","确认本店发货地、运费模板与服务承诺","确认原图使用权及生成图的商品一致性"],"content_id":cid,"platform":c.platform,"market":c.market,"risk_level":c.risk_level,"version":v.version,"status":v.status,"fields":json.loads(v.visible_json),"issues":json.loads(v.issues_json),"changes":json.loads(v.changes_json),"rule":{"version":rule["version"],"source_type":rule["source_type"],"source_url":rule["source_url"],"verified_at":"2026-09-09"},"notice":"生成结果不代表平台审核通过"}
def edit_platform(root,cid,fields):
    current=get_platform_content(root,cid)
    if set(fields)!=set(REQUIRED[current["platform"]]):raise ValueError("平台字段不完整")
    with session_scope(root) as s:
        c=s.get(PlatformContent,cid); n=c.current_version+1;c.current_version=n;issues=scan(fields,{},c.platform);c.risk_level="高" if issues else "低"
        s.add(PlatformContentVersion(content_id=cid,version=n,visible_json=json.dumps(fields,ensure_ascii=False),issues_json=json.dumps(issues,ensure_ascii=False),changes_json="[]",status="草稿",model="human-edit",created_at=utcnow()))
    return get_platform_content(root,cid)
def confirm_platform(root,cid):
    with session_scope(root) as s:
        c=s.get(PlatformContent,cid);v=s.scalar(select(PlatformContentVersion).where(PlatformContentVersion.content_id==cid,PlatformContentVersion.version==c.current_version));v.status="已确认"
    return get_platform_content(root,cid)
