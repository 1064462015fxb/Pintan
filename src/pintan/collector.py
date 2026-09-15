"""1688采集 Agent：保存页面证据、确定性提取字段，并可让Deep Agent辅助归一化。"""
from __future__ import annotations
import hashlib, json, re, uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from sqlalchemy import select
from .asset_records import find_source, update_metadata
from .database import CollectionRun, CollectedFact, CollectedSku, Product, ProductAsset, ProductClaim, session_scope, utcnow
from .collector_mapping import CATALOG, clean_blocks, parse_mapping, persist_mapping, confirm_mapping

PROMPT_VERSION="collector-v2.1"
FIELDS=tuple(CATALOG)

def validate_1688_offer_url(url:str)->str:
    """只接受标准详情页，拒绝凭据、端口、搜索页及伪造子域名。"""
    try:p=urlsplit(url.strip())
    except ValueError:raise ValueError("1688链接格式无效") from None
    if p.scheme not in {"http","https"} or p.hostname!="detail.1688.com" or p.username or p.password or p.port not in {None,80,443}:raise ValueError("仅支持1688商品详情链接")
    match=re.fullmatch(r"/offer/(\d+)\.html",p.path)
    if not match:raise ValueError("链接必须是 detail.1688.com/offer/数字.html")
    return match.group(1)

def _clean(value:Any,limit:int=4000)->str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]","",str(value or "")).strip()[:limit]

def _rules(snapshot:dict[str,Any])->dict[str,Any]:
    title=_clean(snapshot.get("title"),300)
    title=re.sub(r"\s*[-–—]\s*(阿里巴巴|1688).*$","",title).strip()
    text=_clean(snapshot.get("visible_text"),40000)
    facts={key:'' for key in FIELDS};facts['title']=title
    patterns={"minimum_order_text":r"([0-9]+\s*(?:件|个|包|套)\s*起批)",
              "material":r"(?:材质|主要材质)\s*[:：]?\s*([^\n]{1,30})"}
    for field,pattern in patterns.items():
        m=re.search(pattern,text,re.I); facts[field]=m.group(1).strip() if m else ""
    return {"facts":facts,"skus":snapshot.get("skus",[]) if isinstance(snapshot.get("skus"),list) else []}

def collect_snapshot(root:Path,snapshot:dict[str,Any],use_model:bool=True,run_id:str|None=None)->dict[str,Any]:
    url=_clean(snapshot.get("page_url"),2000);offer_id=validate_1688_offer_url(url)
    if run_id:
        with session_scope(root) as s:
            existing=s.get(CollectionRun,run_id)
            if existing:
                if existing.offer_id!=offer_id:raise ValueError('运行ID与商品不一致')
                return get_run(root,run_id)
    image_urls=[];image_details={}
    details=snapshot.get('images',[]) if isinstance(snapshot.get('images'),list) else []
    for item in details[:300]:
        if not isinstance(item,dict):continue
        value=_clean(item.get('url'),2000)
        evidence={k:_clean(item.get(k),500) for k in ('region','sku','source_locator','discovery')}
        # 一张图可同时出现在主图/SKU/详情区；SKU关系证据优先，但保留全部区域证据。
        prior=image_details.get(value,{})
        origins=prior.get('region_evidence',[])+[evidence]
        priorities={'SKU图':0,'主图':1,'详情图':2}
        chosen=min(origins,key=lambda e:priorities.get(e.get('region'),3))
        image_details[value]=dict(chosen,region_evidence=origins)
    old=snapshot.get('image_urls',[]) if isinstance(snapshot.get('image_urls'),list) else []
    for raw in list(image_details)+old[:300]:
        value=_clean(raw,2000)
        try:p=urlsplit(value)
        except ValueError:continue
        if p.scheme in {"http","https"} and p.hostname and not p.username and not p.password and value not in image_urls:image_urls.append(value)
    safe={"capture_version":_clean(snapshot.get("capture_version"),20) or "legacy","page_url":url,"title":_clean(snapshot.get("title"),300),"visible_text":_clean(snapshot.get("visible_text"),40000),"image_urls":image_urls[:60],"skus":snapshot.get("skus",[])[:100] if isinstance(snapshot.get("skus"),list) else []}
    safe['image_urls']=image_urls[:300];image_urls=safe['image_urls']
    safe['text_blocks']=clean_blocks(snapshot)
    safe['images']=[dict(url=u,**image_details.get(u,{})) for u in image_urls]
    safe['image_diagnostics']={'found':len(image_urls),'message':'文字已保存，未发现图片；请使用扩展重试或补充图片地址' if not image_urls else '已发现图片', 'regions':[_clean(x,1000) for x in snapshot.get('diagnostics',[])[:20]] if isinstance(snapshot.get('diagnostics'),list) else []}
    normalized=_rules(safe);model="rules";error_type=None
    mapped=parse_mapping(safe,run_id or 'preview')
    # 专用区域优先于整页正则；公司名称不再写入商品标题。
    if safe['text_blocks']:
        normalized['facts']={k:mapped['resolved'].get(k) or '' for k in FIELDS}
        normalized['skus']=mapped['skus']
    elif not mapped['resolved']['title']:
        normalized['facts']['title']=''
    if not safe['title'] and not safe['visible_text'] and not image_urls:
        raise ValueError('仅有链接，尚未读取商品页面。请在Chrome/Edge的1688商品页使用采集助手提交，或在手动导入中填写页面文字与图片地址。')
    # 模型是可选增强；失败时保留确定性结果，不影响证据入库。
    if use_model:
        try:
            from .agents import _json,_make_agent,_skill_trace,_text
            agent,model=_make_agent(root,"collector","你是1688字段归一化Agent。只能整理输入证据，缺失字段置空，输出facts和skus JSON。")
            response=agent.invoke({"messages":[{"role":"user","content":json.dumps({"snapshot":safe,"allowed_fields":FIELDS},ensure_ascii=False)}]},{"recursion_limit":10})
            _skill_trace(response,"data-collector"); candidate=_json(_text(response))
            if isinstance(candidate,dict) and isinstance(candidate.get("facts"),dict):
                # 只接收白名单字段；标题必须能在页面证据中找到，避免模型改写商品。
                facts={k:_clean(candidate["facts"].get(k),500) for k in FIELDS}
                for field,value in facts.items():
                    if value and value not in (safe["title"]+safe["visible_text"]+'\n'.join(b['text'] for b in safe['text_blocks'])):raise ValueError("模型字段不在页面证据中: "+field)
                # 模型空值不能抹除已提取且有页面证据的字段。
                normalized={"facts":{k:normalized['facts'][k] or (facts[k] if k!='title' else '') for k in FIELDS},"skus":normalized["skus"]}
        except Exception as exc:model="rules-fallback";error_type=type(exc).__name__
    run_id=run_id or "CR-"+uuid.uuid4().hex;product_id="1688-"+offer_id;now=utcnow()
    canonical_url=f"https://detail.1688.com/offer/{offer_id}.html"
    facts=normalized["facts"];title=facts.get("title") or f"1688商品 {offer_id}"
    with session_scope(root) as s:
        product=s.get(Product,product_id)
        if not product:s.add(Product(product_id=product_id,title=title,category=facts.get("category", ""),source_platform="1688",source_product_url=canonical_url,verification_status="待复核"))
        else:product.source_product_url=canonical_url
        s.add(CollectionRun(run_id=run_id,page_url=url,offer_id=offer_id,product_id=product_id,status="待复核",raw_snapshot_json=json.dumps(safe,ensure_ascii=False),normalized_json=json.dumps(normalized,ensure_ascii=False),model=model,prompt_version=PROMPT_VERSION,error_type=error_type,created_at=now,updated_at=now))
        for field,value in facts.items():
            if not value:continue
            fid="CF-"+hashlib.sha256(f"{run_id}|{field}|{value}".encode()).hexdigest()[:20]
            locator="page.title" if field=="title" else "page.visible_text"
            s.add(CollectedFact(fact_id=fid,run_id=run_id,product_id=product_id,field_name=field,raw_value=value,normalized_value=value,source_locator=locator,confidence=1.0 if field=="title" else .75,verification_status="待复核"))
            # 待复核采集事实留在采集层；内容可引用事实由人工确认时发布。
        added=0;reused=0;asset_ids=[]
        for i,image_url in enumerate(image_urls):
            aid="AS-"+hashlib.sha256(f"{product_id}|{image_url}".encode()).hexdigest()[:16]
            evidence=image_details.get(image_url,{})
            region=evidence.get('region')
            # 评论区图片属于详情素材，但保留“评论图”区域证据，
            # 后续不得用它证明供应商规格或官方功效。
            kind='详情图' if region=='评论图' else (region if region in {'主图','SKU图','详情图'} else '待分类商品图')
            asset=find_source(s,product_id,image_url)
            if asset:
                reused+=1;aid=asset.asset_id
                if kind!='待分类商品图':asset.asset_type=kind
            else:
                added+=1;s.add(ProductAsset(asset_id=aid,product_id=product_id,asset_type=kind,status='待下载',source_url=image_url,authorization_status='未确认'));s.flush()
            if evidence:update_metadata(s,aid,**evidence,capture_run_id=run_id,review_included=None)
            asset_ids.append(aid)
        run=s.get(CollectionRun,run_id)
        safe['image_diagnostics'].update(added=added,reused=reused)
        safe['asset_ids']=list(dict.fromkeys(asset_ids))
        safe['regions']=snapshot.get('regions',[])
        run.raw_snapshot_json=json.dumps(safe,ensure_ascii=False)
        mapping=persist_mapping(s,run,safe)
        normalized['mapping_version']=mapping['mapping_version']
        normalized['conflicts']=mapping['conflicts']
        run.normalized_json=json.dumps(normalized,ensure_ascii=False)
        for i,sku in enumerate(normalized.get("skus",[]) if isinstance(normalized.get("skus"),list) else []):
            sid="SKU-"+hashlib.sha256(f"{run_id}|{i}|{json.dumps(sku,sort_keys=True,ensure_ascii=False)}".encode()).hexdigest()[:20]
            text=str(sku.get('text','')) if isinstance(sku,dict) else str(sku)
            mapped_sku=next((x for x in mapping['skus'] if x['text']==text.strip()[:1000]),{})
            if isinstance(sku,dict):sku={**sku,'evidence_id':mapped_sku.get('evidence_id'),'scope':mapped_sku.get('scope')}
            image=sku.get('image_url','') if isinstance(sku,dict) else ''
            try:ip=urlsplit(image)
            except ValueError:ip=urlsplit('')
            image=image if ip.scheme in {'http','https'} and ip.hostname and not ip.username and not ip.password else None
            s.add(CollectedSku(sku_id=sid,run_id=run_id,product_id=product_id,spec_json=json.dumps(sku,ensure_ascii=False),price_cny=mapped_sku.get('price_cny'),stock=mapped_sku.get('stock'),image_url=image,verification_status="待复核"))
    return get_run(root,run_id)

def get_run(root:Path,run_id:str)->dict[str,Any]:
    with session_scope(root) as s:
        run=s.get(CollectionRun,run_id)
        if not run:raise ValueError("采集记录不存在")
        facts=list(s.scalars(select(CollectedFact).where(CollectedFact.run_id==run_id).order_by(CollectedFact.field_name)))
        assets=list(s.scalars(select(ProductAsset).where(ProductAsset.product_id==run.product_id).order_by(ProductAsset.asset_id)))
        return {"run_id":run.run_id,"page_url":run.page_url,"offer_id":run.offer_id,"product_id":run.product_id,"status":run.status,"model":run.model,"prompt_version":run.prompt_version,"created_at":run.created_at,"facts":[{"field_name":x.field_name,"raw_value":x.raw_value,"normalized_value":x.normalized_value,"source_locator":x.source_locator,"confidence":x.confidence,"verification_status":x.verification_status} for x in facts],"images":[{"asset_id":x.asset_id,"source_url":x.source_url,"status":x.status} for x in assets]}

def confirm_run(root:Path,run_id:str)->dict[str,Any]:
    with session_scope(root) as s:
        run=s.get(CollectionRun,run_id)
        if not run:raise ValueError("采集记录不存在")
        snapshot=json.loads(run.raw_snapshot_json)
        if not snapshot.get('title') and not snapshot.get('visible_text') and not snapshot.get('image_urls'):
            raise ValueError('这条记录没有商品内容，无法确认；请重新采集')
        confirm_mapping(s,run)
        # 分类修改和排除只在最终确认时发布。原文件及来源记录仍保留。
        captured=set(snapshot.get('asset_ids',[]))
        for asset in s.scalars(select(ProductAsset).where(ProductAsset.product_id==run.product_id)):
            from .asset_records import metadata
            meta=metadata(s,asset.asset_id)
            if asset.asset_id not in captured and meta.get('review_run_id')!=run_id:continue
            if meta.get('review_run_id') not in (None,run_id):continue
            auto_pass=(asset.asset_type=='待分类商品图' and bool(asset.width and asset.height)
                       and max(asset.width,asset.height)<=256)
            if meta.get('review_included') is False or auto_pass:
                asset.status='复核排除'
            elif meta.get('review_asset_type') in {'主图','SKU图','详情图','功能图','参数图','场景图','待分类商品图'}:
                asset.asset_type=meta['review_asset_type']
        run.status="已人工复核";run.updated_at=utcnow()
        # 先进入候选；完整评分后才会晋级稳定池。
        from .selection_intake import enroll_candidate
        enroll_candidate(s,run)
    return get_run(root,run_id)

def auto_enroll_run(root:Path,run_id:str)->dict[str,Any]:
    """批量文字采集成功后进入待评分候选，不冒充稳定池商品。"""
    with session_scope(root) as s:
        run=s.get(CollectionRun,run_id)
        if not run:raise ValueError("采集记录不存在")
        # 不调用人工确认发布逻辑；这里只把当前有证据的映射送入选品池。
        from .selection_intake import enroll_candidate
        enroll_candidate(s,run)
        from .database import BusinessRow
        row=s.get(BusinessRow,('candidate',run.product_id))
        data=json.loads(row.payload)
        try:pairs=[x for x in json.loads(data.get('spec_price_pairs') or '[]') if x.get('price_cny') is not None]
        except (TypeError,ValueError):pairs=[]
        if not str(data.get('title') or '').strip() or not pairs:
            raise ValueError('缺少可靠商品标题或带单价的SKU')
        data['verification_status']='批量候选·待评分'
        data['batch_auto_enrolled']=True
        row.payload=json.dumps(data,ensure_ascii=False)
        run.status='批量候选·待评分';run.updated_at=utcnow()
    return get_run(root,run_id)
