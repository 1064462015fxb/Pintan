"""仅监听本机的轻量调试服务。密钥不会进入任何 API 响应。"""
from __future__ import annotations

import json
import base64
import threading
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, HTTPException, Body
from fastapi.responses import FileResponse

from .agents import run_stable_agent, run_trending_agent
from .data_store import SEARCH_FILE, STABLE_FILE, TREND_FILE, read_csv
from .trending import latest_match, read_details, run_top3_explanations
from .content_master import confirm_version, get_master, list_masters, run_content_master, save_version
from .database import (AgentTask, AngleDiagnosis, AssetAuthorization, BusinessRow, ContentMaster,
    GeneratedMedia, ImagePlan, ImageReview, MediaTask, PlatformContent, PlatformContentVersion,
    Product, ProductAsset, StableScore, init_database, product_bundle, session_scope, utcnow)
from .content_flow import confirm_platform, diagnose_angle, edit_platform, generate_platform, get_platform_content, save_choice
from .media_pipeline import (authorize_asset, authorize_product_assets, collect_assets, confirm_media, generate_images,
    media_product, resolve_asset_file, resolve_media_file, upload_asset)
from .collector import collect_snapshot, confirm_run, get_run
from .collector_mapping import CATALOG, read_mapping, save_review
from .collector_enrichment import run_collection_enrichment
from .collector_images import download_asset, quality
from .database import CollectionRun, CollectorTask
from .asset_records import metadata, resolve, update_metadata
from .image_workflow import image_source_identity
from .content_readiness import readiness as content_readiness, require_ready
from .task_control import Cancellation, current as cancellation_context
from sqlalchemy import select
from .workbench import with_covers, install_routes as install_workbench


def with_product_link(row: dict, field: str = "product_url") -> dict:
    """仅暴露详情字段；搜索入口不可替代商品详情，不回写业务表。"""
    url = str(row.get(field) or "").strip()
    try:
        p = urlsplit(url)
        valid = (p.scheme in {"http", "https"} and bool(p.hostname)
                 and not p.username and not p.password and "\\" not in url
                 and not any(c.isspace() or ord(c) < 32 for c in url)
                 and "search" not in p.path.lower() and not p.hostname.startswith("s."))
        _ = p.port
    except ValueError:
        valid = False
    return {**row, "detail_url": url if valid else None,
            "detail_link_status": "available" if valid else "unavailable"}


def create_app(root: Path) -> FastAPI:
    init_database(root)
    # 手工数据模式会禁止 CSV 回灌，但采集映射升级仍必须执行。否则解析器
    # 已修复后，历史页面仍可能显示旧的小数价格或空 SKU 列表。
    from .collector_mapping import backfill_mappings
    backfill_mappings(root)
    # v5 不再允许不完整记录停留在稳定池。启动时幂等重算：完整候选晋级，
    # 旧的不完整稳定记录迁回候选，同时保留采集、素材及评分历史。
    from .stable import score_and_promote_candidates
    score_and_promote_candidates(root)
    with session_scope(root) as session:
        for old in session.scalars(select(AgentTask).where(AgentTask.status.in_(["queued","running"]))):
            old.status="interrupted"; old.updated_at=utcnow()
        for old in session.scalars(select(MediaTask).where(MediaTask.status.in_(["queued","running"]))):
            old.status="interrupted"; old.error_type="service_restart"
            old.result_json=json.dumps({**json.loads(old.result_json or '{}'),'error':'服务重启导致图片任务中断；历史成功图片已保留，可重新生成。'},ensure_ascii=False)
            old.updated_at=utcnow()
    app = FastAPI(title="品探双 Agent 调试台")
    from .collector_bridge import install_collector_bridge
    install_collector_bridge(app, root)
    jobs: dict[str, dict[str, Any]] = {}
    media_jobs: dict[str, dict[str, Any]] = {}
    media_cancellations = {}
    guard = threading.RLock()

    def job(task_id: str, kind: str, trend_id: str | None = None, force: bool = False) -> None:
        try:
            with guard: jobs[task_id]["status"] = "running"
            with session_scope(root) as session:
                task=session.get(AgentTask,task_id); task.status="running"; task.updated_at=utcnow()
            if kind == "stable": result = run_stable_agent(root)
            elif kind == "details": result = run_top3_explanations(root, trend_id or "")
            elif kind == "content":
                product_id, _, context = (trend_id or "").partition("|")
                result = run_content_master(root, product_id, context or None, force=force)
            elif kind == "angle":
                payload=json.loads(trend_id or "{}"); result=diagnose_angle(root,payload["product_id"],payload.get("trend_id"),force)
            elif kind == "platform":
                payload=json.loads(trend_id or "{}"); result=generate_platform(root,payload["diagnosis_id"],payload["choice_id"],payload["platform"],force)
            elif kind == "collector-enrich": result=run_collection_enrichment(root,trend_id or "")
            elif kind=='stable-estimate':
                from .stable_estimation import estimate_stable
                result=estimate_stable(root,trend_id or '',force)
            else: result = run_trending_agent(root, trend_id or "", force=force)
            with guard: jobs[task_id].update(status="completed", result=result)
            with session_scope(root) as session:
                task=session.get(AgentTask,task_id); task.status="completed"; task.updated_at=utcnow()
        except Exception as exc:
            safe_detail=(str(exc)[:500] if kind in {"collector-enrich","content","angle","platform"} and isinstance(exc,ValueError)
                         else "任务未完成，原结果保留")
            with guard: jobs[task_id].update(status="failed", error=f"{type(exc).__name__}: {safe_detail}")
            with session_scope(root) as session:
                task=session.get(AgentTask,task_id); task.status="failed"; task.error_type=type(exc).__name__; task.updated_at=utcnow()

    def submit(background: BackgroundTasks, kind: str, trend_id: str | None = None, force: bool = False):
        # 在排队时就保留 kind/输入范围，原子检查和登记，避免后台尚未启动时重复提交。
        group = "stable" if kind == "stable" else (f"collector:{trend_id}" if kind=="collector-enrich" else (f"content:{trend_id}" if kind in {"content","angle","platform"} else "trending"))
        if kind=='stable-estimate':group=f'stable-estimate:{trend_id}'
        with guard:
            for task_id, existing in jobs.items():
                if existing["group"] != group or existing["status"] not in {"queued", "running"}: continue
                if (existing["kind"], existing["trend_id"], existing["force"]) == (kind, trend_id, force):
                    return {"task_id": task_id, "deduplicated": True}
                raise HTTPException(409, "同类任务正在运行，请等待完成")
            task_id = uuid.uuid4().hex
            jobs[task_id] = {"status": "queued", "kind": kind, "group": group, "trend_id": trend_id, "force": force, "created_at": utcnow()}
            with session_scope(root) as session:
                session.add(AgentTask(task_id=task_id,kind=kind,subject_id=trend_id or "",status="queued",created_at=utcnow(),updated_at=utcnow()))
            background.add_task(job, task_id, kind, trend_id, force)
            return {"task_id": task_id, "deduplicated": False}

    app.state.jobs = jobs
    app.state.submit_task = submit

    def media_job(task_id:str,kind:str,payload:dict)->None:
        """媒体任务与Agent任务分开，避免长时图片请求阻塞文本流程。"""
        token=media_cancellations[task_id]
        context=cancellation_context.set(token)
        try:
            with guard:
                if media_jobs.get(task_id,{}).get("status")=="cancelled":
                    return
                media_jobs[task_id]["status"]="running"
            with session_scope(root) as s:
                row=s.get(MediaTask,task_id); row.status="running"; row.updated_at=utcnow()
            if kind=="collect":result=collect_assets(root,payload["product_id"])
            elif kind=="collector-download":
                result=download_asset(root,payload["asset_id"])
                from .image_workflow import classify_product
                result['classification']=classify_product(root,payload['product_id'],asset_ids=[payload['asset_id']])
            elif kind=="image-classify":
                from .image_workflow import classify_product
                result=classify_product(root,payload['product_id'],force=payload.get('force',False))
            elif kind=="image-plan":
                from .image_workflow import plan_images,is_text_only_concept_slot
                result=plan_images(root,payload['content_id'])
                # 只在接口响应中标注自动纯场景槽位，不改写已经持久化的规划JSON。
                result={**result,'slots':[{**slot,'automatic_generation_mode':
                    ('text_only_concept' if is_text_only_concept_slot(slot) else None)} for slot in result.get('slots',[])]}
            elif kind=="images":
                from .image_workflow import generate_planned
                def image_progress(done,total,workers,success=0,failed=0):
                    with guard:
                        token.check()
                        media_jobs[task_id].update(completed_slots=done,total_slots=total,parallel_workers=workers,success_count=success,failed_count=failed)
                    with session_scope(root) as s:
                        row=s.get(MediaTask,task_id);row.result_json=json.dumps(
                            {'progress':{'completed_slots':done,'total_slots':total,'parallel_workers':workers,'success_count':success,'failed_count':failed}},ensure_ascii=False)
                        row.updated_at=utcnow()
                result=generate_planned(root,payload["content_id"],payload.get("slots"),
                    replan=payload.get('replan',False),avoid_asset_ids=payload.get('avoid_asset_ids'),
                    audit_level=payload.get('audit_level','normal'),
                    generation_mode=payload.get('generation_mode','conservative'),
                    slot_modes=payload.get('slot_modes'),user_feedback=payload.get('user_feedback',''),
                    parent_media_id=payload.get('parent_media_id'),progress_callback=image_progress)
            elif kind=="video-plan":
                from .video_workflow import plan_video
                result=plan_video(root,payload["content_id"],payload.get("generation_mode","conservative"),
                    payload.get("aspect_ratio","auto"),payload.get("force",False))
            elif kind=="video":
                from .video_workflow import generate_video
                def video_progress(done,total,workers,success=0,failed=0):
                    with guard:
                        token.check();media_jobs[task_id].update(completed_slots=done,total_slots=total,
                            parallel_workers=workers,success_count=success,failed_count=failed)
                    with session_scope(root) as s:
                        row=s.get(MediaTask,task_id);row.result_json=json.dumps(
                            {'progress':{'completed_slots':done,'total_slots':total,'parallel_workers':workers,
                            'success_count':success,'failed_count':failed}},ensure_ascii=False);row.updated_at=utcnow()
                result=generate_video(root,payload["content_id"],payload.get("plan_id"),payload.get("shot_indexes"),video_progress)
            else:raise ValueError("未知媒体任务")
            with guard:
                token.check()
                with session_scope(root) as s:
                    row=s.get(MediaTask,task_id);row.status="completed";row.result_json=json.dumps(result,ensure_ascii=False);row.updated_at=utcnow()
                media_jobs[task_id].update(status="completed",result=result)
        except Exception as exc:
            with guard:
                if token.event.is_set():return
                with session_scope(root) as s:
                    row=s.get(MediaTask,task_id);row.status="failed";row.error_type=type(exc).__name__
                    row.result_json=json.dumps({**json.loads(row.result_json or '{}'),'error':str(exc)[:500]},ensure_ascii=False);row.updated_at=utcnow()
                media_jobs[task_id].update(status="failed",error=f"{type(exc).__name__}: {str(exc)}")
        finally:
            cancellation_context.reset(context)
            with guard:media_jobs[task_id]['worker_finished']=True

    def submit_media(background:BackgroundTasks,kind:str,payload:dict):
        product_id=str(payload.get("product_id","") or "")
        content_id=str(payload.get("content_id","") or "")
        fingerprint=json.dumps({"kind":kind,"payload":payload},ensure_ascii=False,sort_keys=True)
        import hashlib
        input_hash=hashlib.sha256(fingerprint.encode()).hexdigest()
        with guard:
            for task_id,item in media_jobs.items():
                if item["input_hash"]==input_hash and item["status"] in {"queued","running"}:
                    return {"task_id":task_id,"deduplicated":True}
            if sum(not item.get('worker_finished') for item in media_jobs.values())>=8:
                raise HTTPException(429,'媒体处理并发已满，请稍后重试；已取消的在途请求释放资源后即可继续')
            task_id=uuid.uuid4().hex;media_jobs[task_id]={"status":"queued","kind":kind,"input_hash":input_hash,
                "content_id":content_id,"product_id":product_id,"created_at":utcnow()}
            media_cancellations[task_id]=Cancellation()
            with session_scope(root) as s:s.add(MediaTask(task_id=task_id,kind=kind,product_id=product_id,
                content_id=content_id or None,input_hash=input_hash,status="queued",created_at=utcnow(),updated_at=utcnow()))
            background.add_task(media_job,task_id,kind,payload)
        return {"task_id":task_id,"deduplicated":False}

    app.state.submit_media = submit_media

    @app.get('/api/health')
    async def health():
        return {'status':'ok'}

    @app.get("/")
    def index(): return FileResponse(root / "src/pintan/static/index.html", headers={"Cache-Control":"no-store"})

    @app.get("/app.js")
    def script(): return FileResponse(root / "src/pintan/static/app.js", media_type="application/javascript", headers={"Cache-Control":"no-store"})

    @app.get('/workbench.js')
    def workbench_script(): return FileResponse(root/'src/pintan/static/workbench.js',media_type='application/javascript',headers={'Cache-Control':'no-store'})

    @app.get('/workbench.css')
    def workbench_style(): return FileResponse(root/'src/pintan/static/workbench.css',media_type='text/css',headers={'Cache-Control':'no-store'})

    @app.post("/api/collector/capture")
    def collector_capture(payload:dict=Body(...)):
        """接收浏览器侧快照；请求中不得包含Cookie、Authorization或请求头。"""
        forbidden={"cookie","cookies","authorization","headers","local_storage"}
        if forbidden.intersection({str(k).lower() for k in payload}):raise HTTPException(422,"快照不得包含登录凭据或请求头")
        try:return collect_snapshot(root,payload,bool(payload.get("use_model",True)))
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.get("/api/collector/runs")
    def collector_history():
        with session_scope(root) as s:
            rows=list(s.scalars(select(CollectionRun).order_by(CollectionRun.created_at.desc(),CollectionRun.run_id.desc()).limit(100)))
            return {"items":[{"run_id":r.run_id,"product_id":r.product_id,"title":(s.get(Product,r.product_id).title if r.product_id and s.get(Product,r.product_id) else r.offer_id),"status":r.status,"created_at":r.created_at} for r in rows]}

    @app.get("/api/collector/runs/{run_id}")
    def collector_run(run_id:str):
        try:
            result=get_run(root,run_id)
            with session_scope(root) as s:
                snapshot=json.loads(s.get(CollectionRun,run_id).raw_snapshot_json)
                normalized=json.loads(s.get(CollectionRun,run_id).normalized_json)
                result['conflicts']=normalized.get('conflicts',[])
                result['skus']=normalized.get('skus',[])
                result['regions']=snapshot.get('regions',[])
                result['mapping']=read_mapping(s,run_id)
                # 兼容原接口，新增结构化 SKU 供采集页和后续采购情境使用。
                from .database import CollectedSku, AssetRequirement
                product_assets=list(s.scalars(select(ProductAsset).where(ProductAsset.product_id==result['product_id'])))
                # 一个1688图片源常同时存在 80/220/800 等URL变体。SKU快照可能
                # 记录到220px预览，但同一轮采集已经下载了800px文件。按源身份
                # 汇总后选择实际像素最大的本地文件，不能依赖数据库返回顺序。
                assets_by_identity={}
                for candidate in product_assets:
                    capture=metadata(s,candidate.asset_id)
                    source=capture.get('best_source_url') or candidate.source_url
                    identity=image_source_identity(source)
                    resolved=resolve(s,candidate)
                    rank=(bool(resolved.local_path),
                          int(resolved.width or 0)*int(resolved.height or 0),
                          int(resolved.width or 0),int(resolved.height or 0))
                    previous=assets_by_identity.get(identity)
                    if previous is None or rank>previous[0]:
                        assets_by_identity[identity]=(rank,resolved)
                sku_records=[]
                for sku in s.scalars(select(CollectedSku).where(CollectedSku.run_id==run_id)):
                    linked=None
                    if sku.image_url:
                        identity=image_source_identity(sku.image_url)
                        matched=assets_by_identity.get(identity)
                        linked=matched[1] if matched else None
                    sku_records.append({'sku_id':sku.sku_id,'spec':json.loads(sku.spec_json),
                        'price_cny':sku.price_cny,'stock':sku.stock,'image_url':sku.image_url,
                        'sku_asset_id':linked.asset_id if linked else None,
                        'sku_preview_url':f'/api/assets/{linked.asset_id}/file' if linked and linked.local_path else None,
                        'verification_status':sku.verification_status})
                result['sku_records']=sku_records
                sku_linked_asset_ids={x['sku_asset_id'] for x in sku_records if x.get('sku_asset_id')}
                result['can_confirm']=bool(snapshot.get('title') or snapshot.get('visible_text'))
                download_results={}
                for task in s.scalars(select(CollectorTask).where(CollectorTask.offer_id==result['offer_id']).order_by(CollectorTask.created_at.desc()).limit(100)):
                    progress=json.loads(task.payload)
                    if progress.get('run_id')==run_id:
                        download_results={x['asset_id']:x for x in progress.get('image_results',[])}
                        break
                current_ids=set(snapshot.get('asset_ids',[]))
                # 旧快照按其原始URL找回本次素材；别名跟随到有效ID，不重复展示。
                for a in s.scalars(select(ProductAsset).where(ProductAsset.product_id==result['product_id'])):
                    if a.asset_id in current_ids or a.source_url in snapshot.get('image_urls',[]):
                        current_ids.add(resolve(s,a).asset_id)
                result['image_diagnostics']=snapshot.get('image_diagnostics',{'found':len(snapshot.get('image_urls',[])),'message':'文字已保存，未发现图片；请重新使用扩展采集' if not snapshot.get('image_urls') else '历史采集记录'})
                result['images']=[]
                for a in s.scalars(select(ProductAsset).where(ProductAsset.product_id==result['product_id'])):
                    if a.status.startswith('已合并:'):continue
                    capture=metadata(s,a.asset_id)
                    regions={str(x.get('region','')) for x in capture.get('region_evidence',[])}
                    if capture.get('region'):regions.add(str(capture['region']))
                    # 人工复核优先；若尚未人工指定，同图兼具主图与SKU证据时
                    # 标签显示SKU图。分类优先级不删除主图区域证据。
                    display_type=capture.get('review_asset_type') or (
                        'SKU图' if ('SKU图' in regions or a.asset_id in sku_linked_asset_ids) else a.asset_type)
                    result['images'].append({'asset_id':a.asset_id,'asset_type':display_type,'source_url':a.source_url if a.source_url and a.source_url.startswith(('https://','http://')) else None,'status':a.status,'width':a.width,'height':a.height,'quality':quality(a.width,a.height,display_type) if a.width and a.height else '未下载','preview_url':f'/api/assets/{a.asset_id}/file' if a.local_path else None,'authorization_status':a.authorization_status,'ocr_text':a.ocr_text or ''})
                    result['images'][-1]['capture_metadata']=capture
                    result['images'][-1]['review_asset_type']=display_type
                    result['images'][-1]['review_included']=result['images'][-1]['capture_metadata'].get('review_included',True)
                    # 兼容修复前已下载的历史记录：响应时也计算同一条自动PASS规则。
                    # 不删除文件与来源，只从正式素材区和后续内容管线排除。
                    from .collector_images import is_review_image
                    result['images'][-1]['is_review_image']=is_review_image(capture)
                    result['images'][-1]['auto_passed']=(
                        bool(a.width and a.height) and
                        quality(a.width,a.height,display_type)=='缩略图' and
                        not result['images'][-1]['is_review_image'])
                    result['images'][-1]['auto_pass_reason']=(
                        '非评论区缩略图，已过滤且不进入OCR/模型'
                        if result['images'][-1]['auto_passed'] else None)
                    result['images'][-1]['current_run']=a.asset_id in current_ids
                    result['images'][-1]['current_download']=download_results.get(a.asset_id)
                # “缺失素材需求”不是对已下载图片的计数，而是内容生产所需的
                # 功能、参数、场景三类素材槽。旧采集记录没有初始化占位行时，
                # 在响应中补一个可追溯的虚拟需求，避免新记录错误显示为 0。
                # 用户上传或采到同类图片后，下面的关联逻辑会将其显示为“已找到”。
                existing_blank={x['asset_type'] for x in result['images']
                                if not x['source_url'] and not x['preview_url']}
                requirements=list(s.scalars(select(AssetRequirement).where(AssetRequirement.product_id==result['product_id'])))
                for asset_type in [x.asset_type for x in requirements] or ['功能图','参数图','场景图']:
                    if asset_type not in existing_blank:
                        result['images'].append({'asset_id':f'REQ-{run_id}-{asset_type}',
                            'asset_type':asset_type,'source_url':None,'status':'待详情采集',
                            'width':None,'height':None,'quality':'未下载','preview_url':None,
                            'authorization_status':'未确认','capture_metadata':{'source_kind':'系统素材需求',
                            'requirement_for':'内容生产'},'current_run':True,'current_download':None,
                            'virtual_requirement':True})
                for item in result['images']:
                    # 没有来源和本地文件的记录才是“缺失需求”。人工上传的图片
                    # 也可填补需求；同一文件被指定为多个用途时读取其用途关联。
                    if item['source_url'] or item['preview_url']:continue
                    match=next((a for a in result['images'] if a['preview_url'] and a.get('review_included',True)
                        and not a.get('auto_passed') and
                        (a['asset_type']==item['asset_type'] or item['asset_type'] in
                         a.get('capture_metadata',{}).get('manual_target_asset_types',[]))),None)
                    if match:item.update(status='已找到',linked_asset_id=match['asset_id'])
            return result
        except ValueError as exc:raise HTTPException(404,str(exc)) from None

    @app.post("/api/collector/assets/{asset_id}/download")
    def collector_download(asset_id:str):
        try:return download_asset(root,asset_id)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.post('/api/collector/assets/{asset_id}/tasks',status_code=202)
    def collector_download_task(asset_id:str,background:BackgroundTasks):
        with session_scope(root) as s:
            asset=s.get(ProductAsset,asset_id)
            if not asset or not asset.source_url:raise HTTPException(404,'没有可下载的图片来源')
            pid=asset.product_id
        return submit_media(background,'collector-download',{'product_id':pid,'asset_id':asset_id})

    @app.post("/api/collector/runs/{run_id}/confirm")
    def collector_confirm(run_id:str,background:BackgroundTasks):
        try:
            result=confirm_run(root,run_id)
            with session_scope(root) as s:pid=s.get(CollectionRun,run_id).product_id
            from .stable_estimation import refresh_inputs
            data=refresh_inputs(root,pid)
            try:has_sku_price=any(x.get('price_cny') is not None for x in json.loads(data.get('spec_price_pairs') or '[]'))
            except (TypeError,ValueError):has_sku_price=False
            if has_sku_price:result['scoring_task']=submit(background,'stable-estimate',pid)
            return result
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.post('/api/collector/runs/{run_id}/review')
    def collector_review(run_id:str,payload:dict=Body(...)):
        try:
            with session_scope(root) as s:
                fields=save_review(s,run_id,payload.get('fields'))
            return {'run_id':run_id,'status':'复核草稿已保存','fields':fields}
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.post('/api/collector/runs/{run_id}/ai-complete',status_code=202)
    def collector_ai_complete(run_id:str,background:BackgroundTasks):
        """AI只生成缺失字段的复核草稿；真实采集字段和最终发布状态不变。"""
        with session_scope(root) as s:
            run=s.get(CollectionRun,run_id)
            if not run:raise HTTPException(404,'采集记录不存在')
            if run.status=='已人工复核':raise HTTPException(409,'已确认记录不能补全，请重新采集生成新版本')
        return submit(background,'collector-enrich',run_id)

    @app.post('/api/collector/runs/{run_id}/assets/{asset_id}/review')
    def collector_asset_review(run_id:str,asset_id:str,payload:dict=Body(...)):
        allowed={'主图','SKU图','详情图','信息图','功能图','参数图','场景图','待分类商品图'}
        kind=str(payload.get('asset_type') or '')
        if kind not in allowed:raise HTTPException(422,'图片分类无效')
        with session_scope(root) as s:
            run=s.get(CollectionRun,run_id);asset=s.get(ProductAsset,asset_id)
            if not run or not asset or asset.product_id!=run.product_id:raise HTTPException(404,'采集图片不存在')
            update_metadata(s,asset_id,review_run_id=run_id,review_asset_type=kind,
                            review_included=bool(payload.get('included',True)))
        return {'asset_id':asset_id,'asset_type':kind,'included':bool(payload.get('included',True)),'status':'复核草稿已保存'}

    @app.get("/api/stable")
    def stable():
        rows = read_csv(root / STABLE_FILE); rows.sort(key=lambda r: int(r.get("stable_rank") or 9999))
        latest = root / "outputs/stable_latest.json"
        report = ""
        if latest.exists():
            try: report = json.loads(latest.read_text(encoding="utf-8")).get("report", "")
            except (ValueError, OSError): report = ""
        if not rows:report='暂无已确认的稳定商品，请先在采集模块复核并确认入库。'
        elif not any(r.get('total_score') not in (None,'') for r in rows):
            from .stable import deterministic_report
            report=deterministic_report(rows)
        items = with_covers(root, [with_product_link(r) for r in rows])
        return {"top10": items[:10], "items": items, "scored": bool(rows and rows[0].get("total_score")), "report": report}

    @app.get('/api/selection/candidates')
    def selection_candidates():
        with session_scope(root) as s:
            rows=[json.loads(x.payload) for x in s.scalars(
                select(BusinessRow).where(BusinessRow.kind=='candidate'))]
        rows.sort(key=lambda x:(x.get('promotion_status')=='rejected',
                                -float(x.get('total_score') or 0),x.get('title') or ''))
        return {'items':with_covers(root,[with_product_link(x) for x in rows]),
                'count':len(rows),'version':'stable-rules-v5-three-pillars'}

    @app.post('/api/selection/candidates/{product_id}/inputs')
    def candidate_inputs(product_id:str,background:BackgroundTasks,payload:dict=Body(...)):
        return stable_inputs(product_id,background,payload)

    @app.post('/api/selection/candidates/{product_id}/estimate',status_code=202)
    def candidate_estimate(product_id:str,background:BackgroundTasks,payload:dict=Body(default={})):
        with session_scope(root) as s:
            if not s.get(BusinessRow,('candidate',product_id)):raise HTTPException(404,'待评分候选不存在')
        return submit(background,'stable-estimate',product_id,bool(payload.get('force',False)))

    @app.post('/api/stable/{product_id}/inputs')
    def stable_inputs(product_id:str,background:BackgroundTasks,payload:dict=Body(...)):
        from .selection_intake import update_scoring_inputs
        try:
            rows=update_scoring_inputs(root,product_id,payload)
        except (ValueError,TypeError) as exc:raise HTTPException(400,str(exc)) from None
        result={'item':next((r for r in rows if r['stable_id']==product_id),None)}
        if not result['item']:raise HTTPException(404,'选品商品不存在')
        try:has_sku_price=any(x.get('price_cny') is not None for x in json.loads(result['item'].get('spec_price_pairs') or '[]'))
        except (TypeError,ValueError):has_sku_price=False
        if has_sku_price and not result['item'].get('ai_estimate'):
            result['scoring_task']=submit(background,'stable-estimate',product_id)
        return result

    @app.post('/api/stable/{product_id}/estimate',status_code=202)
    def stable_estimate(product_id:str,background:BackgroundTasks,payload:dict=Body(default={})):
        return submit(background,'stable-estimate',product_id,bool(payload.get('force',False)))

    @app.post('/api/stable/{product_id}/restore-price')
    def restore_price(product_id:str):
        from .database import BusinessRow
        from .stable import score_stable_products
        with session_scope(root) as s:
            row=(s.get(BusinessRow,('candidate',product_id)) or
                 s.get(BusinessRow,('stable',product_id)))
            if not row:raise HTTPException(404,'商品不存在')
            data=json.loads(row.payload)
            suggested=data.get('ai_sku_reference_prices') or {}
            if not suggested:raise HTTPException(422,'尚无各SKU的AI建议售价')
            data['sku_reference_prices']=suggested
            data['manual_sku_reference_prices']={}
            data['reference_retail_price_cny']=str(round(sum(float(x) for x in suggested.values())/len(suggested),2))
            data['manual_scoring_fields']=[k for k in data.get('manual_scoring_fields',[]) if k!='reference_retail_price_cny']
            row.payload=json.dumps(data,ensure_ascii=False)
        return {'items':score_stable_products(root)}

    @app.get("/api/trends")
    def trends(): return {"items": with_covers(root,[with_product_link(r) for r in read_csv(root / TREND_FILE)])}

    @app.get("/api/trends/{trend_id}")
    def trend(trend_id: str):
        record = next((x for x in read_csv(root / TREND_FILE) if x["trend_id"] == trend_id), None)
        if not record: raise HTTPException(404, "爆款不存在")
        rows = [x for x in read_csv(root / SEARCH_FILE) if x["trend_id"] == trend_id]
        eligible=[x for x in rows if x.get('gate_status')!='淘汰']
        rejected=[x for x in rows if x.get('gate_status')=='淘汰']
        ranked = sorted((x for x in eligible if x.get("match_rank")), key=lambda x: int(x["match_rank"]))
        try: latest = latest_match(root, trend_id)
        except ValueError: latest = None
        metadata = {k: latest.get(k) for k in ("cache_hit", "cache_key", "run_id", "elapsed_seconds", "generated_at", "generation_seconds", "stale")} if latest else None
        details = read_details(root, latest) if latest else None
        return {"trend": with_covers(root,[with_product_link(record)])[0], "candidate_count": len(eligible), "top3": with_covers(root,[with_product_link(r, "offer_url") for r in ranked[:3]]), "all10": with_covers(root,[with_product_link(r, "offer_url") for r in eligible[:10]]),
                "rejected":with_covers(root,[with_product_link(r,"offer_url") for r in rejected]),"metadata": metadata, "details": details}

    @app.post("/api/tasks/stable", status_code=202)
    def start_stable(background: BackgroundTasks):
        return submit(background, "stable")

    @app.post("/api/tasks/trending/{trend_id}", status_code=202)
    def start_trending(trend_id: str, background: BackgroundTasks, force: bool = False):
        if not any(x["trend_id"] == trend_id for x in read_csv(root / TREND_FILE)): raise HTTPException(404, "爆款不存在")
        return submit(background, "trending", trend_id, force)

    @app.post("/api/tasks/trending/{trend_id}/explanations", status_code=202)
    def start_details(trend_id: str, background: BackgroundTasks):
        try: latest = latest_match(root, trend_id)
        except ValueError: raise HTTPException(404, "爆款不存在或输入不完整") from None
        if not latest or latest["stale"]: raise HTTPException(409, "请先匹配当前输入，再生成详解")
        return submit(background, "details", trend_id)

    @app.get("/api/tasks/{task_id}")
    def task(task_id: str):
        with guard: data=dict(jobs.get(task_id,{}))
        with session_scope(root) as s: row=s.get(AgentTask,task_id)
        if not row: raise HTTPException(404, '任务不存在')
        return {**data,'status':row.status,'kind':row.kind,'stage':row.kind,'created_at':row.created_at,
                'updated_at':row.updated_at,'error':data.get('error') or ('服务已重启，请重新开始' if row.status=='interrupted' else None)}

    @app.get("/api/content/candidates")
    def content_candidates(trend_id: str | None = None):
        stable = read_csv(root / STABLE_FILE)
        stable.sort(key=lambda r: int(r.get("stable_rank") or 9999))
        def stable_product_id(row):
            if row.get("source_supply_id"):return row["source_supply_id"]
            sid=row["stable_id"]
            if sid.startswith("ST-DAY-"):return "1688-"+sid.removeprefix("ST-DAY-")
            if sid.startswith("ST-1688-"):return sid.removeprefix("ST-")
            return sid
        result=[{"selection_id":"stable:"+r["stable_id"],"product_id":stable_product_id(r),"trend_id":None,"title":r["title"],"source":"稳定选品","rank":r.get("stable_rank"),"default_selected":i<3} for i,r in enumerate(stable)]
        if trend_id:
            rows=[r for r in read_csv(root/SEARCH_FILE) if r["trend_id"]==trend_id]
            rows.sort(key=lambda r:int(r.get("match_rank") or 9999))
            result += [{"selection_id":"trend:"+r["search_id"],"product_id":r["source_supply_id"],"trend_id":trend_id,"title":r["title"],"source":"爆款推荐","rank":r.get("match_rank"),"default_selected":i<3 and bool(r.get("match_rank"))} for i,r in enumerate(rows)]
        known={x["product_id"] for x in result}
        with session_scope(root) as s:
            test_products=list(s.scalars(select(Product).join(ProductAsset).where(ProductAsset.local_path.is_not(None)).distinct()))
        result += [{"selection_id":"asset:"+p.product_id,"product_id":p.product_id,"trend_id":None,
                    "title":p.title,"source":"已入库测试素材","rank":None,"default_selected":False}
                   for p in test_products if p.product_id not in known]
        # 前端以 product_id 去重，优先保留带爆款上下文的候选。
        return {"items":with_covers(root,result)}

    @app.get("/api/content/products/{product_id}/readiness")
    def product_readiness(product_id:str):
        try:return content_readiness(root,product_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None

    @app.post("/api/content/products/{product_id}/prepare",status_code=202)
    async def prepare_product(product_id:str):
        with session_scope(root) as s:
            product=s.get(Product,product_id)
            if not product:raise HTTPException(404,"商品不存在")
            url=product.source_product_url
        if not url:raise HTTPException(409,{"code":"missing_source_url","stage":"prepare","blockers":[{"code":"missing_source_url","message":"商品没有1688详情地址"}],"next_action":"fix_source_url"})
        return await app.state.collector_bridge.submit(url,"assets")

    @app.post("/api/content/products/{product_id}/confirm-facts")
    def confirm_product_facts(product_id:str,payload:dict=Body(default={})):
        state=content_readiness(root,product_id);run_id=str(payload.get("run_id") or state["facts"].get("run_id") or "")
        if not run_id:raise HTTPException(409,{"code":"missing_collection_run","stage":"review_facts","blockers":[{"code":"missing_collection_run","message":"没有可确认的素材采集记录"}],"next_action":"prepare"})
        with session_scope(root) as s:
            run=s.get(CollectionRun,run_id)
            if not run or run.product_id!=product_id:raise HTTPException(422,"采集记录与商品不匹配")
        try:return {"result":confirm_run(root,run_id),"readiness":content_readiness(root,product_id)}
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.post("/api/content/products/{product_id}/authorize-assets")
    def authorize_product(product_id:str,payload:dict=Body(...)):
        try:
            result=authorize_product_assets(root,product_id,list(payload.get("asset_ids") or []),
                bool(payload.get("confirmed")),str(payload.get("statement") or ""))
            return {"result":result,"readiness":content_readiness(root,product_id)}
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.get("/api/content/masters")
    def masters(): return {"items":[{"master_id":x["master_id"],"product_id":x["product_id"],"status":x["status"],"current_version":x["current_version"],"created_at":x["created_at"]} for x in list_masters(root)]}

    @app.get("/api/content/masters/{master_id}")
    def master(master_id:str): raise HTTPException(410,"内部母版不再向前端公开，请读取平台内容")

    @app.post("/api/tasks/content", status_code=202)
    def start_content(background:BackgroundTasks,payload:dict=Body(...)):
        raise HTTPException(410,"请先进行风格诊断，再生成平台内容")

    @app.post("/api/content/masters/{master_id}/versions")
    def edit_master(master_id:str,payload:dict=Body(...)): raise HTTPException(410,"内部母版不可由前端编辑")

    @app.post("/api/content/masters/{master_id}/versions/{version}/confirm")
    def approve_master(master_id:str,version:int): raise HTTPException(410,"请确认平台内容版本")

    @app.post("/api/tasks/content/diagnose",status_code=202)
    def start_diagnosis(background:BackgroundTasks,payload:dict=Body(...)):
        product_id=str(payload.get("product_id","")).strip(); trend_id=payload.get("trend_id")
        product_bundle(root,product_id,trend_id)
        try:require_ready(root,product_id,"diagnose")
        except ValueError as exc:raise HTTPException(409,getattr(exc,"readiness",{"code":"content_not_ready","stage":"diagnose"})) from None
        subject=json.dumps({"product_id":product_id,"trend_id":trend_id},ensure_ascii=False,sort_keys=True)
        return submit(background,"angle",subject,bool(payload.get("force",False)))

    @app.post("/api/content/choices")
    def choose(payload:dict=Body(...)):
        try:return save_choice(root,str(payload.get("diagnosis_id","")),list(payload.get("selected_angles",[])),str(payload.get("custom_angle","") or ""))
        except ValueError as e:raise HTTPException(422,str(e)) from None

    @app.post("/api/tasks/content/platform",status_code=202)
    def start_platform(background:BackgroundTasks,payload:dict=Body(...)):
        with session_scope(root) as s:
            diagnosis=s.get(AngleDiagnosis,str(payload.get("diagnosis_id","") or ""))
            if not diagnosis:raise HTTPException(404,"内容风格诊断不存在")
            product_id=diagnosis.product_id
        try:require_ready(root,product_id,"platform_content")
        except ValueError as exc:raise HTTPException(409,getattr(exc,"readiness",{"code":"content_not_ready","stage":"platform_content"})) from None
        subject=json.dumps({k:payload.get(k) for k in ("diagnosis_id","choice_id","platform")},ensure_ascii=False,sort_keys=True)
        return submit(background,"platform",subject,bool(payload.get("force",False)))

    @app.get('/api/content/products/{product_id}/latest')
    def latest_product_content(product_id:str):
        from .database import PlatformContent,ContentMaster
        with session_scope(root) as s:
            content=s.scalar(select(PlatformContent).join(ContentMaster,ContentMaster.master_id==PlatformContent.master_id).where(ContentMaster.product_id==product_id).order_by(PlatformContent.created_at.desc()))
            cid=content.content_id if content else None
        return {'result':get_platform_content(root,cid) if cid else None}

    @app.get("/api/content/platform/{content_id}")
    def platform_result(content_id:str):
        try:return get_platform_content(root,content_id)
        except ValueError as e:raise HTTPException(404,str(e)) from None

    @app.post("/api/content/platform/{content_id}/versions")
    def edit_result(content_id:str,payload:dict=Body(...)):
        try:return edit_platform(root,content_id,payload)
        except ValueError as e:raise HTTPException(422,str(e)) from None

    @app.post("/api/content/platform/{content_id}/confirm")
    def confirm_result(content_id:str):
        try:return confirm_platform(root,content_id)
        except (ValueError,AttributeError) as e:raise HTTPException(404,str(e)) from None

    @app.post("/api/assets/collect",status_code=202)
    def collect(background:BackgroundTasks,payload:dict=Body(...)):
        product_id=str(payload.get("product_id","")).strip()
        try:product_bundle(root,product_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None
        return submit_media(background,"collect",{"product_id":product_id})

    @app.post("/api/assets/{asset_id}/authorize")
    def authorize(asset_id:str,payload:dict=Body(...)):
        try:return authorize_asset(root,asset_id,bool(payload.get("confirmed")),str(payload.get("statement","") or ""))
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.post("/api/assets/upload")
    def upload(background:BackgroundTasks,payload:dict=Body(...)):
        try:
            encoded=str(payload.get("data_base64","") or "")
            # Base64最多约为原始文件的4/3；先限制字符串，避免恶意请求占满内存。
            if len(encoded)>14*1024*1024:raise ValueError("上传图片超过10MB")
            raw=base64.b64decode(encoded,validate=True)
            result=upload_asset(root,str(payload.get("product_id","")).strip(),raw,
                                str(payload.get("mime_type","") or ""),str(payload.get("asset_type","") or "人工上传原图"))
            run_id=str(payload.get('run_id') or '')
            if run_id:
                with session_scope(root) as s:
                    run=s.get(CollectionRun,run_id);asset=s.get(ProductAsset,result['asset_id'])
                    if not run or not asset or run.product_id!=asset.product_id:raise ValueError('上传图片与采集记录不匹配')
                    update_metadata(s,asset.asset_id,review_run_id=run_id,review_asset_type=result.get('asset_type',asset.asset_type),review_included=True)
            result['classification_task']=submit_media(background,'image-classify',{'product_id':str(payload.get('product_id',''))})
            return result
        except (ValueError,base64.binascii.Error) as exc:raise HTTPException(422,str(exc)) from None

    @app.get("/api/assets/{asset_id}/file")
    def asset_file(asset_id:str):
        try:return FileResponse(resolve_asset_file(root,asset_id))
        except ValueError as exc:raise HTTPException(404,str(exc)) from None

    @app.post('/api/collector/products/{product_id}/classify',status_code=202)
    def classify_images(product_id:str,background:BackgroundTasks,payload:dict=Body(default={})):
        with session_scope(root) as s:
            if not s.get(Product,product_id):raise HTTPException(404,'商品不存在')
        return submit_media(background,'image-classify',{'product_id':product_id,'force':bool(payload.get('force',False))})

    @app.get('/api/collector/products/{product_id}/classifications')
    def classifications(product_id:str):
        from .image_workflow import analyses
        return {'items':analyses(root,product_id)}

    @app.post('/api/media/plan',status_code=202)
    def image_plan(background:BackgroundTasks,payload:dict=Body(...)):
        content_id=str(payload.get('content_id',''))
        with session_scope(root) as s:
            content=s.get(PlatformContent,content_id);master=s.get(ContentMaster,content.master_id) if content else None
        if not master:raise HTTPException(404,'平台内容对应商品不存在')
        try:require_ready(root,master.product_id,'image_planning')
        except ValueError as exc:raise HTTPException(409,getattr(exc,'readiness',{'code':'content_not_ready','stage':'image_planning'})) from None
        return submit_media(background,'image-plan',{'content_id':content_id})

    @app.get('/api/media/reviews/{review_id}/file')
    def review_file(review_id:str):
        with session_scope(root) as s:row=s.get(ImageReview,review_id)
        if not row:raise HTTPException(404,'审核记录不存在')
        relative=json.loads(row.payload).get('file')
        if not relative:raise HTTPException(404,'本轮没有生成图片')
        path=(root/relative).resolve()
        try:path.relative_to((root/'data/media').resolve())
        except ValueError:raise HTTPException(404,'审核图片路径无效') from None
        if not path.is_file():raise HTTPException(404,'审核图片不存在')
        return FileResponse(path)

    @app.post("/api/media/images",status_code=202)
    def images(background:BackgroundTasks,payload:dict=Body(...)):
        content_id=str(payload.get("content_id","")).strip()
        audit_level=str(payload.get('audit_level','normal')).strip()
        from .image_workflow import audit_profile,normalize_generation_mode,normalize_slot_modes
        try:
            audit_profile(audit_level);generation_mode=normalize_generation_mode(payload.get('generation_mode'))
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        try:current=get_platform_content(root,content_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None
        with session_scope(root) as s:
            content=s.get(PlatformContent,content_id);master=s.get(ContentMaster,content.master_id) if content else None
        if not master:raise HTTPException(404,"平台内容对应商品不存在")
        try:require_ready(root,master.product_id,"image_generation")
        except ValueError as exc:raise HTTPException(409,getattr(exc,"readiness",{"code":"content_not_ready","stage":"image_generation"})) from None
        with session_scope(root) as s:
            plan=s.scalar(select(ImagePlan).where(ImagePlan.content_id==content_id).order_by(ImagePlan.created_at.desc()))
            labels=set()
            if plan:
                saved=json.loads(plan.payload);labels={f"{x['section']}-{x['index']}" for x in saved.get('slots',[])}
        try:slot_modes=normalize_slot_modes(payload.get('slot_modes'),labels if plan else set())
        except ValueError as exc:raise HTTPException(422,str(exc)) from None
        if slot_modes and not plan:raise HTTPException(422,'请先查看AI图片规划，再设置单图覆盖')
        return submit_media(background,"images",{"content_id":content_id,"slots":payload.get("slots"),
            'audit_level':audit_level,'generation_mode':generation_mode,'slot_modes':slot_modes})

    @app.post("/api/media/images/{media_id}/retry",status_code=202)
    def retry_image(media_id:str,background:BackgroundTasks,payload:dict=Body(default={})):
        feedback=payload.get('user_feedback','')
        if not isinstance(feedback,str) or len(feedback)>1000:raise HTTPException(422,'改进意见最多1000字')
        with session_scope(root) as s:item=s.get(GeneratedMedia,media_id)
        if not item or item.media_type!="image" or not item.content_id:raise HTTPException(404,"图片记录不存在")
        meta=json.loads(item.metadata_json);resolution=meta.get('resolution') or {}
        replan=False
        avoid=meta.get('source_asset_ids',[]) if replan else []
        actual=meta.get('generation_mode','conservative')
        retained=actual if actual in {'conservative','creative'} else meta.get('requested_generation_mode','conservative')
        return submit_media(background,"images",{"content_id":item.content_id,"slots":[item.slot],
            'replan':replan,'avoid_asset_ids':avoid,
            'audit_level':(meta.get('audit_profile') or {}).get('level','normal'),
            'user_feedback':feedback.strip(),'parent_media_id':media_id,
            'generation_mode':retained,'slot_modes':({item.slot:actual} if actual in {'conservative','creative'} else {})})

    @app.post("/api/media/video/plan",status_code=202)
    def video_plan(background:BackgroundTasks,payload:dict=Body(...)):
        content_id=str(payload.get("content_id","")).strip()
        try:get_platform_content(root,content_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None
        mode=str(payload.get('generation_mode','conservative'));ratio=str(payload.get('aspect_ratio','auto'))
        if mode not in {'conservative','creative'}:raise HTTPException(422,'短视频模式无效')
        if ratio not in {'auto','9:16','1:1','16:9'}:raise HTTPException(422,'短视频比例无效')
        return submit_media(background,"video-plan",{"content_id":content_id,'generation_mode':mode,
            'aspect_ratio':ratio,'force':bool(payload.get('force',False))})

    @app.get("/api/media/video/plan")
    def video_plan_latest(content_id:str):
        from .video_workflow import latest_video_plan
        value=latest_video_plan(root,content_id)
        return {'plan':value}

    @app.post("/api/media/video",status_code=202)
    def video(background:BackgroundTasks,payload:dict=Body(...)):
        content_id=str(payload.get("content_id","")).strip();plan_id=str(payload.get('plan_id','')).strip()
        try:get_platform_content(root,content_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None
        if not plan_id:raise HTTPException(422,'请先生成并确认15秒分镜')
        return submit_media(background,"video",{"content_id":content_id,'plan_id':plan_id})

    @app.post("/api/media/video/{plan_id}/retry",status_code=202)
    def video_retry(plan_id:str,background:BackgroundTasks,payload:dict=Body(default={})):
        from .video_workflow import video_plan
        try:plan=video_plan(root,plan_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None
        indexes=payload.get('shot_indexes')
        if indexes is None:indexes=[x['index'] for x in plan['shots'] if x['status']!='success']
        if not isinstance(indexes,list) or not indexes:raise HTTPException(422,'没有需要重试的镜头')
        try:indexes=sorted({int(x) for x in indexes})
        except (TypeError,ValueError):raise HTTPException(422,'镜头编号无效') from None
        if any(x<1 or x>5 for x in indexes):raise HTTPException(422,'镜头编号必须在1到5之间')
        return submit_media(background,"video",{"content_id":plan['content_id'],'plan_id':plan_id,'shot_indexes':indexes})

    @app.get('/api/media/tasks')
    def active_media_tasks(content_id:str, include_finished:bool=False):
        with session_scope(root) as s:
            query=select(MediaTask).where(MediaTask.content_id==content_id)
            if not include_finished:query=query.where(MediaTask.status.in_(['queued','running']))
            rows=s.scalars(query.order_by(MediaTask.created_at.desc()).limit(20)).all()
            return {'items':[{'task_id':r.task_id,'status':r.status,'kind':r.kind,
                'created_at':r.created_at} for r in rows]}

    @app.get("/api/media/tasks/{task_id}")
    def media_task(task_id:str):
        with guard:
            live=dict(media_jobs.get(task_id,{}))
        with session_scope(root) as s:row=s.get(MediaTask,task_id)
        if not row:raise HTTPException(404,"媒体任务不存在")
        result=json.loads(row.result_json) if row.result_json else None
        error=(result or {}).get('error') if isinstance(result,dict) else None
        progress=(result or {}).get('progress',{}) if isinstance(result,dict) else {}
        items=(result or {}).get('items',[]) if isinstance(result,dict) else []
        if row.kind in {'images','video'} and row.status=='completed':
            progress.update(completed_slots=len(items),total_slots=len(items),success_count=sum(x.get('status')=='success' for x in items),failed_count=sum(x.get('status')!='success' for x in items))
        return {**live,**progress,"status":row.status,"kind":row.kind,"stage":row.kind,"created_at":row.created_at,"updated_at":row.updated_at,"result":result,
                "error":error or (f"{row.error_type}: 任务未完成" if row.error_type else None)}

    @app.post("/api/media/tasks/{task_id}/cancel")
    def cancel_media_task(task_id:str):
        """Cancel a queued/running media job without deleting earlier media versions."""
        message='已取消任务，停止后续处理；在途模型请求可能继续计费，但返回后不会发布。历史图片保留。'
        with guard:
            with session_scope(root) as s:
                row=s.get(MediaTask,task_id)
                if not row:raise HTTPException(404,'媒体任务不存在')
                if row.status in {'completed','failed','interrupted','cancelled'}:
                    return {'task_id':task_id,'status':row.status,'message':'任务已结束，未执行额外操作。'}
                token=media_cancellations.get(task_id)
                if token:token.cancel()
                row.status='cancelled';row.error_type='user_cancelled'
                row.result_json=json.dumps({**json.loads(row.result_json or '{}'),'error':message},ensure_ascii=False);row.updated_at=utcnow()
            item=media_jobs.get(task_id)
            if item:item.update(status='cancelled',error=message)
        return {'task_id':task_id,'status':'cancelled','message':message}

    @app.get("/api/media/products/{product_id}")
    def media_for_product(product_id:str):
        return media_product(root,product_id)

    @app.get("/api/distribution/ready")
    def distribution_ready():
        """Return only locally produced, reviewable listing packages.

        This endpoint intentionally has no publishing side effect: final confirmation in
        QianNiu/Jingmai (category, inventory, freight template, and store account) remains
        a human action.
        """
        with session_scope(root) as s:
            packages=[]
            contents=list(s.scalars(select(PlatformContent).order_by(PlatformContent.created_at.desc())))
            for content in contents:
                master=s.get(ContentMaster,content.master_id)
                if not master:
                    continue
                product=s.get(Product,master.product_id)
                version=s.scalar(select(PlatformContentVersion).where(
                    PlatformContentVersion.content_id==content.content_id,
                    PlatformContentVersion.version==content.current_version,
                ))
                # A platform entry without a generated textual version cannot be handed off.
                if not product or not version:
                    continue
                assets=list(s.scalars(select(ProductAsset).where(ProductAsset.product_id==product.product_id)))
                authorizations={(x.asset_id,x.asset_sha256) for x in s.scalars(
                    select(AssetAuthorization).where(AssetAuthorization.confirmed.is_(True))
                )}
                usable=[x for x in assets if x.local_path and x.status!='复核排除' and not x.status.startswith('已合并:')]
                authorized=sum(1 for x in usable if x.sha256 and (x.asset_id,x.sha256) in authorizations)
                latest_media={}
                rows=s.scalars(select(GeneratedMedia).where(
                    GeneratedMedia.content_id==content.content_id,
                    GeneratedMedia.status=='success',
                ).order_by(GeneratedMedia.created_at.desc())).all()
                for item in rows:
                    if json.loads(item.metadata_json or '{}').get('content_version') != version.version:
                        continue
                    key=(item.media_type,item.slot)
                    if key in latest_media:
                        continue
                    latest_media[key]={
                        'media_id':item.media_id, 'media_type':item.media_type, 'slot':item.slot,
                        'version':item.version, 'confirmed':item.confirmed, 'created_at':item.created_at,
                        'url':f'/api/media/files/{item.media_id}' if item.local_path else None,
                        'metadata':json.loads(item.metadata_json or '{}'),
                    }
                stable=s.get(StableScore,product.product_id)
                stable_payload=json.loads(stable.payload) if stable else {}
                packages.append({
                    'content_id':content.content_id,
                    'product':{'product_id':product.product_id,'title':product.title,'category':product.category,
                               'source_url':product.source_product_url,'verification_status':product.verification_status},
                    'platform':content.platform,'market':content.market,'risk_level':content.risk_level,
                    'content_version':version.version,'content_status':version.status,'created_at':version.created_at,
                    'fields':json.loads(version.visible_json or '{}'),'issues':json.loads(version.issues_json or '[]'),
                    'stable_score':stable_payload.get('total_score'),
                    'assets':{'usable':len(usable),'authorized':authorized},
                    'media':list(latest_media.values()),
                    'publication_checklist':['确认目标类目及店铺必填属性','逐SKU确认零售价与库存','确认发货地、运费模板与服务承诺','在平台后台预览后由人工发布'],
                    'notice':'本页只整理待发布素材，不会自动发布到店铺。',
                })
        return {'items':packages, 'total':len(packages)}

    @app.get("/api/media/files/{media_id}")
    def media_file(media_id:str):
        try:return FileResponse(resolve_media_file(root,media_id))
        except ValueError as exc:raise HTTPException(404,str(exc)) from None

    @app.post("/api/media/{media_id}/confirm")
    def approve_media(media_id:str):
        try:return confirm_media(root,media_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None
    install_workbench(app,root,distribution_ready)
    return app
