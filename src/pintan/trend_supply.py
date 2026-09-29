"""晋级爆款的1688关键词/图片混合检索与平替评分。"""
from __future__ import annotations

import hashlib
import io
import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote_from_bytes

import httpx
from PIL import Image
from sqlalchemy import select

from .database import (BusinessRow, Product, ProductCover, SearchCandidate,
    TrendDiscoveryBatch, TrendDiscoveryCandidate, session_scope, utcnow, write_business, read_business)
from .media_pipeline import MAX_IMAGE_BYTES, _safe_public_url
from .trend_discovery import score_substitute, stable_id
from .workbench import safe_cover_file


def dumps(value):return json.dumps(value,ensure_ascii=False,sort_keys=True)


def queue_supply_jobs(root:Path,batch_id:str)->int:
    with session_scope(root) as s:
        batch=s.get(TrendDiscoveryBatch,batch_id)
        if not batch:raise ValueError('爆款采集批次不存在')
        result=json.loads(batch.result_json or '{}');jobs=[]
        rows=list(s.scalars(select(TrendDiscoveryCandidate).where(
            TrendDiscoveryCandidate.batch_id==batch_id,TrendDiscoveryCandidate.status=='晋级')))
        for row in rows:
            item=json.loads(row.payload)
            detail=next((x for x in item.get('sources',[]) if x.get('specs_text') or x.get('selling_points')),{})
            detail_terms=(detail.get('specs_text') or detail.get('selling_points') or '')[:60]
            keyword=re.sub(r'\s+',' ',f"{batch.category} {item['title']} {detail_terms}").strip()[:80]
            jobs.append({'job_id':stable_id('TSJ',batch_id,row.trend_id,'keyword'),'trend_id':row.trend_id,
                'method':'keyword','keyword':keyword,'status':'queued','found':0,
                'page_url':'https://s.1688.com/selloffer/offer_search.htm?keywords='+quote_from_bytes(keyword.encode('gb18030'))})
            cover=s.get(ProductCover,row.trend_id);cover_data=json.loads(cover.payload) if cover else {}
            if cover_data.get('path'):
                try:safe_cover_file(root,cover_data['path'])
                except ValueError:pass
                else:
                    jobs.append({'job_id':stable_id('TSJ',batch_id,row.trend_id,'image'),'trend_id':row.trend_id,
                        'method':'image','keyword':'以图搜图','status':'queued','found':0,'page_url':'https://s.1688.com/selloffer/offer_search.htm',
                        'reference_path':cover_data['path'],'reference_token':secrets.token_urlsafe(24)})
        result['supply_jobs']=jobs;result['supply_candidates']={};batch.result_json=dumps(result)
        progress=json.loads(batch.progress_json);progress.update(stage='supply_search',message='正在1688执行关键词与图片混合搜索',supply_search=0)
        batch.progress_json=dumps(progress);batch.status='supply_search' if jobs else 'partial';batch.updated_at=utcnow()
        return len(jobs)


def reference_file(root:Path,batch_id:str,job_id:str,token:str):
    with session_scope(root) as s:
        batch=s.get(TrendDiscoveryBatch,batch_id)
        if not batch:raise ValueError('参考图不存在')
        jobs=json.loads(batch.result_json or '{}').get('supply_jobs',[])
        job=next((x for x in jobs if x.get('job_id')==job_id),None)
    if not job or not secrets.compare_digest(str(token),str(job.get('reference_token',''))):raise ValueError('参考图不存在')
    return safe_cover_file(root,job['reference_path'])


def save_supply_snapshot(root:Path,batch_id:str,job_id:str,candidates:list[dict])->bool:
    with session_scope(root) as s:
        batch=s.get(TrendDiscoveryBatch,batch_id)
        if not batch:raise ValueError('爆款采集批次不存在')
        result=json.loads(batch.result_json or '{}');jobs=result.get('supply_jobs',[])
        job=next((x for x in jobs if x.get('job_id')==job_id),None)
        if not job:raise ValueError('平替搜索任务不存在')
        clean=[]
        for item in candidates[:30]:
            match=re.search(r'/offer/(\d+)\.html',str(item.get('page_url','')))
            if not match:continue
            clean.append({'offer_id':match.group(1),'page_url':f'https://detail.1688.com/offer/{match.group(1)}.html',
                'title':str(item.get('title') or '')[:300],'wholesale_price_cny':item.get('price'),
                'cover_url':str(item.get('cover_url') or '')[:2000],'rank':item.get('relevance_rank'),
                'sales_text':str(item.get('sales_text') or '')[:100],'search_method':job['method']})
        job.update(status='completed',found=len(clean),candidates=clean,lease='')
        done=sum(x.get('status') in {'completed','failed'} for x in jobs)
        progress=json.loads(batch.progress_json);progress.update(supply_search=done,message=f'1688检索完成 {done}/{len(jobs)}')
        batch.result_json=dumps(result);batch.progress_json=dumps(progress);batch.updated_at=utcnow()
        return done==len(jobs)


def _download(root:Path,url:str,identity:str)->Path:
    safe=_safe_public_url(url)
    with httpx.stream('GET',safe,timeout=15,follow_redirects=False) as response:
        response.raise_for_status();mime=response.headers.get('content-type','').split(';')[0]
        if mime not in {'image/jpeg','image/png','image/webp'}:raise ValueError('候选主图格式无效')
        raw=bytearray()
        for chunk in response.iter_bytes():
            raw.extend(chunk)
            if len(raw)>MAX_IMAGE_BYTES:raise ValueError('候选主图过大')
    with Image.open(io.BytesIO(raw)) as image:image.verify()
    folder=root/'data'/'trend-supply';folder.mkdir(parents=True,exist_ok=True)
    path=folder/(hashlib.sha256(identity.encode()).hexdigest()+'.img');path.write_bytes(raw);return path


def _vision_group(reference:Path,items:list[tuple[dict,Path]],trend:dict)->dict[str,dict]:
    from .image_workflow import visual_json
    ids=[x[0]['offer_id'] for x in items]
    prompt=("比较第一张爆款基准图与其后1688候选主图。只依据可见商品结构、外形、部件、用途和规格文字，"
        "不要把背景风格当作同款证据。逐候选输出0-100的image_similarity、feature_match、pain_resolution和一句理由。"
        f"候选图片顺序对应offer_id={ids}；爆款标题={trend.get('title','')}；可核验痛点={trend.get('negative_review_pain_points','')}。"
        'JSON格式：{"assessments":[{"offer_id":"","image_similarity":0,"feature_match":0,"pain_resolution":0,"reason":""}]}')
    value=visual_json(prompt,[reference,*[x[1] for x in items]])
    records=value.get('assessments') if isinstance(value,dict) else None
    if not isinstance(records,list):raise ValueError('视觉评分结构无效')
    output={}
    for item in records:
        identity=str(item.get('offer_id',''))
        if identity not in ids:continue
        scores={key:max(0,min(100,float(item.get(key,0)))) for key in ('image_similarity','feature_match','pain_resolution')}
        output[identity]={**scores,'reason':str(item.get('reason',''))[:500],'source':'qwen-vision'}
    return output


def finalize_supply(root:Path,batch_id:str)->dict:
    with session_scope(root) as s:
        batch=s.get(TrendDiscoveryBatch,batch_id)
        result=json.loads(batch.result_json or '{}');jobs=result.get('supply_jobs',[])
        trend_rows={x.trend_id:json.loads(x.payload) for x in s.scalars(select(TrendDiscoveryCandidate).where(
            TrendDiscoveryCandidate.batch_id==batch_id,TrendDiscoveryCandidate.status=='晋级'))}
        covers={}
        for trend_id in trend_rows:
            row=s.get(ProductCover,trend_id);data=json.loads(row.payload) if row else {}
            if data.get('path'):
                try:covers[trend_id]=safe_cover_file(root,data['path'])
                except ValueError:pass
    all_rows=[];failures=[]
    for trend_id,trend in trend_rows.items():
        merged={}
        for job in jobs:
            if job.get('trend_id')!=trend_id:continue
            for item in job.get('candidates',[]):
                previous=merged.setdefault(item['offer_id'],{**item,'search_methods':[]})
                if item['search_method'] not in previous['search_methods']:previous['search_methods'].append(item['search_method'])
                if not previous.get('cover_url') and item.get('cover_url'):previous['cover_url']=item['cover_url']
        selected=sorted(merged.values(),key=lambda x:(-len(x['search_methods']),x.get('rank') or 999))[:20]
        paths=[]
        for item in selected:
            try:
                if not item.get('cover_url'):raise ValueError('缺少候选主图')
                paths.append((item,_download(root,item['cover_url'],trend_id+'|'+item['offer_id'])))
            except Exception as exc:item['image_error']=type(exc).__name__
        visual={}
        if covers.get(trend_id) and paths:
            groups=[paths[i:i+5] for i in range(0,len(paths),5)]
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(_vision_group,covers[trend_id],group,trend) for group in groups]
                for future in as_completed(futures):
                    try:visual.update(future.result())
                    except Exception as exc:failures.append(type(exc).__name__)
        retail=trend.get('retail_price_cny')
        for item in selected:
            assessment=visual.get(item['offer_id'])
            wholesale=item.get('wholesale_price_cny')
            price_score=None
            if retail not in (None,'') and wholesale not in (None,'') and float(retail)>0:
                price_score=max(0,min(100,(float(retail)-float(wholesale))/float(retail)*100*2))
            score=score_substitute(visual=assessment['image_similarity'] if assessment else 0,
                feature=assessment['feature_match'] if assessment else 0,price=price_score,
                pain=assessment['pain_resolution'] if assessment else 0,supplier=0,
                trend_retail=float(retail) if retail not in (None,'') else None,
                landed_cost=float(wholesale) if wholesale not in (None,'') else None)
            blockers=list(score['blockers'])
            if not assessment:blockers.append('视觉模型未完成，不能晋级')
            qualified=score['qualified'] and bool(assessment)
            all_rows.append({**item,'trend_id':trend_id,'search_id':stable_id('SR',trend_id,item['offer_id']),
                'source_supply_id':'1688-'+item['offer_id'],'category':trend.get('category',''),
                'offer_url':item['page_url'],'data_origin':'browser_visible_evidence','verification_status':'搜索卡证据待复核',
                'feature_tags':item['title'],'estimated_shipping_cny':'0','shipping_evidence':'未采集，暂按0元估算',
                'image_similarity_score':assessment['image_similarity'] if assessment else '',
                'feature_match_score':assessment['feature_match'] if assessment else '',
                'pain_resolution_score':assessment['pain_resolution'] if assessment else '',
                'price_advantage_score':'' if price_score is None else round(price_score,1),'supplier_quality_score':'',
                'match_score':score['match_score'],'gate_status':'合格' if qualified else '淘汰',
                'gate_reasons':'；'.join(blockers),'visual_reason':assessment.get('reason','') if assessment else '',
                'visual_source':assessment.get('source','') if assessment else 'failed'})
    for trend_id in trend_rows:
        qualified=sorted([x for x in all_rows if x['trend_id']==trend_id and x['gate_status']=='合格'],
            key=lambda x:(-float(x['match_score']),x['search_id']))[:10]
        ranks={x['search_id']:i+1 for i,x in enumerate(qualified)}
        for row in all_rows:
            if row['trend_id']==trend_id:row['match_rank']=ranks.get(row['search_id'],'')
    existing=read_business(root,'pintan_trend_search_results.csv');by_id={x['search_id']:x for x in existing}
    with session_scope(root) as s:
        for row in all_rows:
            by_id[row['search_id']]={k:('' if v is None else v) for k,v in row.items()}
            product=s.get(Product,row['source_supply_id'])
            if not product:s.add(Product(product_id=row['source_supply_id'],title=row['title'],category=row['category'],
                source_platform='1688',source_product_url=row['page_url'],verification_status='搜索卡证据待复核'))
            candidate=s.get(SearchCandidate,row['search_id'])
            payload=dumps(row)
            if candidate:candidate.payload=payload
            else:s.add(SearchCandidate(search_id=row['search_id'],trend_id=row['trend_id'],product_id=row['source_supply_id'],payload=payload))
        batch=s.get(TrendDiscoveryBatch,batch_id);progress=json.loads(batch.progress_json)
        qualified=sum(x['gate_status']=='合格' for x in all_rows)
        progress.update(stage='completed',message=f'平替评分完成，合格{qualified}条',supply_scored=len(all_rows),qualified_supply=qualified)
        batch.progress_json=dumps(progress);batch.status='completed' if qualified else 'partial';batch.updated_at=utcnow()
        result=json.loads(batch.result_json);result['supply_summary']={'evaluated':len(all_rows),'qualified':qualified,'failures':failures};batch.result_json=dumps(result)
    write_business(root,'pintan_trend_search_results.csv',list(by_id.values()))
    return {'evaluated':len(all_rows),'qualified':sum(x['gate_status']=='合格' for x in all_rows),'failures':failures}
