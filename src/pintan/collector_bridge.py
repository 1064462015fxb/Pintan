"""本机工作台 ↔ 已配对扩展。

边界：浏览器负责读取页面，服务端只接受白名单证据并下载图片；不接触登录凭据。
任务、快照和逐张结果持久化，WebSocket只承担调度，不能执行任意脚本或命令。
第一版服务必须单进程运行（默认serve就是单worker），锁保护提交与状态转换。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import secrets
import threading
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote_from_bytes, urlsplit

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .asset_records import resolve
from .collector import FIELDS, collect_snapshot, validate_1688_offer_url
from .collector_images import download_asset
from .database import (BatchSelection, BatchSelectionItem, BusinessRow, CollectedFact, CollectionRun,
                       CollectorHelper, CollectorTask, ProductAsset, TrendDiscoveryBatch,
                       DistributionTask, ListingPackage, session_scope, utcnow)

PROTOCOL = 1
VERSION = "2.7.0"
COMPATIBLE_VERSIONS = {'2.0.0','2.1.2','2.3.2','2.4.0','2.4.1','2.4.2','2.4.3','2.4.4','2.5.0','2.6.0',VERSION}  # 旧版仅保留采集兼容；铺货辅助要求最新版。
LABELS = dict(queued="排队", opening="打开网页", reading="读取页面", waiting_user="等待人工处理",
              normalizing="整理资料", downloading="下载图片", completed="完成", partial="部分完成",
              failed="失败", cancelled="已取消", interrupted="已中断")
ACTIVE = {"queued", "opening", "reading", "waiting_user", "normalizing", "downloading"}
TERMINAL = set(LABELS) - ACTIVE
LOCAL_ORIGINS = {"http://127.0.0.1:8010", "http://localhost:8010"}
ERRORS = {
    "login_required": "请在助手所在的Chrome/Edge登录1688，然后点击继续采集。",
    "verification_required": "商品页要求验证，请在Chrome/Edge人工完成后继续。",
    "unrecognized_page": "未识别到商品资料区域，请检查浏览器页面是否正常，处理后继续。",
    "page_timeout": "商品页加载超时，请检查Chrome/Edge页面后继续。",
    "page_changed": "商品标签页已跳转或关闭，请点击继续重新打开目标商品。",
    "capture_timeout": "页面读取超过时间限制，请检查页面后继续。",
    "helper_disconnected": "助手连接中断，请重新连接后显式重试。",
    "server_restarted": "服务已重启，任务中断；已保存资料和图片保留，可显式重试。",
    "image_search_unavailable": "1688当前页面没有识别到可用的以图搜图入口。请在助手浏览器手动打开以图搜图后重试。",
    "invalid_reference_image": "参考图格式或尺寸无效，请选择 PNG、JPG 或 WebP 图片。",
}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ImageEvidence(StrictModel):
    url: str = Field(max_length=2000)
    region: Literal["主图", "SKU图", "详情图", "待分类商品图"]
    sku: str = Field(default="", max_length=500)
    source_locator: str = Field(default="", max_length=500)
    discovery: str = Field(default="DOM", max_length=100)


class SkuEvidence(StrictModel):
    text: str = Field(max_length=1000)
    source_locator: str = Field(max_length=500)
    image_url: str = Field(default="", max_length=2000)
    dimension: str = Field(default="", max_length=100)
    image_relation_type: str = Field(default="", max_length=100)
    displayed_image_changed: bool | None = None


class RegionEvidence(StrictModel):
    region: Literal["主图", "SKU图", "详情图"]
    recognized: bool
    complete: bool
    found: int = Field(ge=0, le=300)
    reason: str = Field(default="", max_length=500)


class TextBlockEvidence(StrictModel):
    region: Literal['title','price','sku','attributes','packaging','supplier','reviews','detail','legacy']
    text: str = Field(max_length=8000)
    source_locator: str = Field(default='',max_length=500)


class PageEvidence(StrictModel):
    capture_version: str = Field(default="legacy", max_length=20)
    page_url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=300)
    visible_text: str = Field(default="", max_length=40000)
    images: list[ImageEvidence] = Field(default_factory=list, max_length=300)
    skus: list[SkuEvidence] = Field(default_factory=list, max_length=100)
    regions: list[RegionEvidence] = Field(default_factory=list, max_length=3)
    diagnostics: list[str] = Field(default_factory=list, max_length=20)
    text_blocks: list[TextBlockEvidence] = Field(default_factory=list,max_length=100)


class UrlSubmission(StrictModel):
    page_url: str = Field(max_length=2000)
    mode: Literal["full", "text_only", "assets"] = "full"


class SearchCandidateEvidence(StrictModel):
    page_url: str = Field(max_length=2000)
    title: str = Field(default="", max_length=300)
    price: float | None = Field(default=None, ge=0)
    sales_text: str = Field(default="", max_length=100)
    moq_text: str = Field(default="", max_length=100)
    is_ad: bool = False
    relevance_rank: int = Field(ge=1, le=500)
    discovery_method: Literal["anchor", "attribute", "embedded-state"] = "anchor"
    cover_url: str = Field(default="", max_length=2000)


class SearchEvidence(StrictModel):
    page_url: str = Field(max_length=2000)
    keyword: str = Field(min_length=1, max_length=80)
    candidates: list[SearchCandidateEvidence] = Field(default_factory=list, max_length=100)
    diagnostics: list[str] = Field(default_factory=list, max_length=20)


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def extension_origin(value):
    return bool(re.fullmatch(r"chrome-extension://[a-p]{32}", value or ""))


def local_write(request: Request):
    """拒绝跨站表单、DNS重绑定以及缺失Origin的远程脚本提交。"""
    if (request.headers.get("origin") not in LOCAL_ORIGINS
            or request.headers.get("host") not in {"127.0.0.1:8010", "localhost:8010"}
            or request.headers.get("content-type", "").split(";")[0] != "application/json"):
        raise HTTPException(403, "只接受本机工作台的JSON请求，请从127.0.0.1:8010打开页面")


class CollectorBridge:
    def __init__(self, root: Path):
        self.root = root
        self.guard = threading.RLock()
        self.connections: dict[str, WebSocket] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self.batch_workers: dict[str, asyncio.Task] = {}
        self.pair_code = ""
        self.pair_until = 0.0
        self.pair_attempts = 0
        self.model_enabled = True  # 测试可关闭，不接受浏览器控制模型配置。
        self.closing = False
        with session_scope(root) as s:
            for helper in s.scalars(select(CollectorHelper)):
                helper.connected = False
            for task in s.scalars(select(CollectorTask).where(CollectorTask.status.in_(ACTIVE))):
                data = json.loads(task.payload)
                data.update(error_type="server_restarted", message=ERRORS["server_restarted"])
                task.status = "interrupted"
                task.payload = dump(data)
                task.updated_at = utcnow()
            for batch in s.scalars(select(BatchSelection).where(BatchSelection.status.in_(
                    {"search_queued","opening_search","reading_search","waiting_user","collecting","estimating"}))):
                data=json.loads(batch.payload);data.update(phase="interrupted",message="服务已重启；已保存候选和商品可继续。")
                batch.status="interrupted";batch.payload=dump(data);batch.updated_at=utcnow()
            for batch in s.scalars(select(TrendDiscoveryBatch).where(TrendDiscoveryBatch.status.in_(
                    {"queued", "collecting", "waiting_user", "supply_search"}))):
                progress=json.loads(batch.progress_json);progress["message"]="服务已重启；已保存平台证据，可继续未完成平台。"
                batch.status="interrupted";batch.progress_json=dump(progress);batch.updated_at=utcnow()
            for task in s.scalars(select(DistributionTask).where(DistributionTask.status.in_(
                    {'opening','filling','waiting_user'}))):
                values=json.loads(task.payload or '{}');values.update(message='服务已重启，请重新发起辅助填写。',error_type='server_restarted')
                task.status='interrupted';task.payload=dump(values);task.updated_at=utcnow()

    def read(self, task_id):
        with self.guard, session_scope(self.root) as s:
            task = s.get(CollectorTask, task_id)
            if not task:
                raise HTTPException(404, "采集任务不存在")
            data = json.loads(task.payload)
            # 快照与一次性调度租约不向工作台返回；凭据从未进入payload。
            return dict(task_id=task.task_id, offer_id=task.offer_id, page_url=task.page_url,
                        helper_id=task.helper_id, status=task.status, status_label=LABELS[task.status],
                        created_at=task.created_at, updated_at=task.updated_at,
                        **{k:v for k,v in data.items() if k not in {"snapshot", "lease", "snapshot_hash"}})

    def change(self, task_id, status=None, **values):
        with self.guard, session_scope(self.root) as s:
            task = s.get(CollectorTask, task_id)
            if not task or task.status in TERMINAL:
                return False
            data = json.loads(task.payload)
            data.update(values)
            task.payload = dump(data)
            task.status = status or task.status
            task.updated_at = utcnow()
            return True

    def bridge_state(self):
        with self.guard, session_scope(self.root) as s:
            helpers = []
            for h in s.scalars(select(CollectorHelper)):
                connected = h.helper_id in self.connections
                busy = s.scalar(select(CollectorTask).where(CollectorTask.helper_id == h.helper_id,
                                                           CollectorTask.status.in_(ACTIVE)))
                listing_busy = s.scalar(select(DistributionTask).where(DistributionTask.helper_id == h.helper_id,
                    DistributionTask.status.in_({'opening','filling','waiting_user'})))
                helpers.append(dict(helper_id=h.helper_id, name=h.name, version=h.version,
                                    connected=connected, busy=bool(connected and (busy or listing_busy)), last_seen=h.last_seen))
            return dict(protocol=PROTOCOL, version=VERSION, helpers=helpers,
                        status="busy" if any(h["busy"] for h in helpers) else "connected" if any(h["connected"] for h in helpers) else "offline")

    def pair(self, code, origin, name, version):
        with self.guard:
            self.pair_attempts += 1
            if (self.pair_attempts > 5 or time.monotonic() >= self.pair_until
                    or not secrets.compare_digest(str(code), self.pair_code)):
                raise HTTPException(403, "配对码无效或过期，请在工作台重新生成")
            if version != VERSION:
                raise HTTPException(409, "助手版本不匹配，请重新加载新版扩展")
            self.pair_until = 0
            token = secrets.token_urlsafe(32)
            helper_id = "CH-" + uuid.uuid4().hex
            with session_scope(self.root) as s:
                s.add(CollectorHelper(helper_id=helper_id, token_hash=hashlib.sha256(token.encode()).hexdigest(),
                                      extension_origin=origin, name=str(name)[:80], version=version,
                                      connected=False, last_seen=utcnow()))
            return dict(helper_id=helper_id, token=token, protocol=PROTOCOL)

    async def submit(self, url, mode="full", batch_id=None, relevance_rank=None):
        try:
            offer = validate_1688_offer_url(url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        with self.guard, session_scope(self.root) as s:
            old = s.scalar(select(CollectorTask).where(CollectorTask.offer_id == offer, CollectorTask.status.in_(ACTIVE)))
            if old:
                return dict(task_id=old.task_id, deduplicated=True)
            if not self.connections:
                raise HTTPException(409, "采集助手未连接。请先在Chrome/Edge加载扩展并配对；没有保存空资料。")
            helper_id = sorted(self.connections)[0]
            task_id = "CT-" + uuid.uuid4().hex
            s.add(CollectorTask(task_id=task_id, helper_id=helper_id, offer_id=offer,
                                page_url=f"https://detail.1688.com/offer/{offer}.html", status="queued",
                                payload=dump(dict(found=0, downloaded=0, failed=0, image_results=[], attempt=1,
                                                  mode=mode,batch_id=batch_id,relevance_rank=relevance_rank)),
                                created_at=utcnow(), updated_at=utcnow()))
        await self.dispatch(helper_id)
        return dict(task_id=task_id, deduplicated=False)

    async def submit_batch(self, original_keyword, normalized_keyword, filters, target_count=20, max_candidates=60):
        from .batch_selection import validate_filters
        original=str(original_keyword or '').strip()[:80];normalized=str(normalized_keyword or '').strip()[:80]
        if not original or not normalized:raise HTTPException(422,"经营方向和搜索词不能为空")
        filters=validate_filters(filters or {})
        target_count=max(1,min(20,int(target_count)));max_candidates=max(target_count,min(60,int(max_candidates)))
        with self.guard, session_scope(self.root) as s:
            latest=[hid for hid in self.connections if s.get(CollectorHelper,hid).version==VERSION]
            if not latest:raise HTTPException(409,f"批量搜索要求采集助手{VERSION}，请重新加载新版扩展")
            signature=hashlib.sha256(dump([normalized,filters,target_count,max_candidates]).encode()).hexdigest()
            for old in s.scalars(select(BatchSelection).where(BatchSelection.status.in_(
                    {"search_queued","opening_search","reading_search","waiting_user","collecting","estimating"}))):
                if json.loads(old.payload).get('signature')==signature:return {'batch_id':old.batch_id,'deduplicated':True}
            helper_id=sorted(latest)[0];batch_id='BS-'+uuid.uuid4().hex
            payload={'signature':signature,'phase':'search_queued','message':'等待助手打开1688搜索页','found':0,
                     # 1688旧搜索入口仍按GB18030解释keywords；UTF-8百分号编码会在搜索框显示乱码。
                     'search_url':'https://s.1688.com/selloffer/offer_search.htm?keywords='+
                                  quote_from_bytes(normalized.encode('gb18030'))}
            s.add(BatchSelection(batch_id=batch_id,helper_id=helper_id,original_keyword=original,
                normalized_keyword=normalized,filters_json=dump(filters),target_count=target_count,
                max_candidates=max_candidates,status='search_queued',payload=dump(payload),created_at=utcnow(),updated_at=utcnow()))
        await self.dispatch(helper_id)
        return {'batch_id':batch_id,'deduplicated':False}

    async def submit_trend_discovery(self, category, filters):
        from .trend_discovery import normalize_filters
        category=str(category or '').strip()[:80]
        if not category: raise HTTPException(422,'请输入品类')
        try: filters=normalize_filters(filters or {})
        except ValueError as exc: raise HTTPException(422,str(exc)) from None
        with self.guard,session_scope(self.root) as s:
            helpers=[hid for hid in self.connections if s.get(CollectorHelper,hid).version==VERSION]
            if not helpers: raise HTTPException(409,f'爆款采集要求采集助手{VERSION}，请重新加载并连接新版扩展')
            helper_id=sorted(helpers)[0];batch_id='TD-'+uuid.uuid4().hex
            platforms={name:{'status':'queued','found':0,'detail_completed':0,'message':'等待采集',
                'surface':'buyin' if name=='douyin' else 'public'} for name in filters['platforms']}
            progress={'stage':'platform_search','message':'等待助手依次读取平台搜索页','platforms':platforms,
                'scored':0,'qualified':0,'supply_search':0,'supply_scored':0}
            s.add(TrendDiscoveryBatch(batch_id=batch_id,helper_id=helper_id,category=category,status='queued',
                filters_json=dump(filters),progress_json=dump(progress),result_json='{}',error_type=None,
                created_at=utcnow(),updated_at=utcnow()))
        await self.dispatch(helper_id)
        return {'batch_id':batch_id,'deduplicated':False}

    def read_trend_discovery(self,batch_id):
        from .trend_discovery import batch_view
        try:return batch_view(self.root,batch_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None

    async def submit_image_batch(self, original_keyword, filters, image_data, mime_type,
                                 target_count=20, max_candidates=60):
        """保存用户提供的参考图，并只把本机受限地址交给已配对的浏览器助手。"""
        from .batch_selection import validate_filters
        original=str(original_keyword or '以图搜图').strip()[:80] or '以图搜图'
        try:
            raw=base64.b64decode(str(image_data or ''), validate=True)
        except (ValueError, TypeError):
            raise HTTPException(422, ERRORS['invalid_reference_image']) from None
        if not 32 <= len(raw) <= 10 * 1024 * 1024:
            raise HTTPException(422, '参考图需介于 32 B 和 10 MB 之间')
        allowed={'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp'}
        mime=str(mime_type or '').lower()
        if mime not in allowed:
            raise HTTPException(422, ERRORS['invalid_reference_image'])
        try:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as image:
                width,height=image.size
                if width < 32 or height < 32 or width * height > 40_000_000:
                    raise ValueError('invalid dimensions')
                if image.format not in {'JPEG','PNG','WEBP'}:
                    raise ValueError('invalid format')
        except Exception:
            raise HTTPException(422, ERRORS['invalid_reference_image']) from None
        filters=validate_filters(filters or {})
        target_count=max(1,min(20,int(target_count)));max_candidates=max(target_count,min(60,int(max_candidates)))
        digest=hashlib.sha256(raw).hexdigest();image_dir=self.root/'data'/'image-search';image_dir.mkdir(parents=True,exist_ok=True)
        path=image_dir/(digest+allowed[mime])
        if not path.exists():path.write_bytes(raw)
        with self.guard, session_scope(self.root) as s:
            latest=[hid for hid in self.connections if s.get(CollectorHelper,hid).version==VERSION]
            if not latest:raise HTTPException(409,f"以图搜图要求采集助手{VERSION}，请重新加载新版扩展")
            signature=hashlib.sha256(dump(['image',digest,filters,target_count,max_candidates]).encode()).hexdigest()
            for old in s.scalars(select(BatchSelection).where(BatchSelection.status.in_(
                    {"search_queued","opening_search","reading_search","waiting_user","collecting","estimating"}))):
                if json.loads(old.payload).get('signature')==signature:return {'batch_id':old.batch_id,'deduplicated':True}
            helper_id=sorted(latest)[0];batch_id='BS-'+uuid.uuid4().hex;token=secrets.token_urlsafe(24)
            payload={'signature':signature,'phase':'search_queued','message':'等待助手打开1688以图搜图页面','found':0,
                     'search_kind':'image','reference_image':{'sha256':digest,'mime_type':mime,'path':str(path.relative_to(self.root)),
                     'width':width,'height':height,'source':'用户上传','token':token}}
            s.add(BatchSelection(batch_id=batch_id,helper_id=helper_id,original_keyword=original,
                normalized_keyword='以图搜图',filters_json=dump(filters),target_count=target_count,max_candidates=max_candidates,
                status='search_queued',payload=dump(payload),created_at=utcnow(),updated_at=utcnow()))
        await self.dispatch(helper_id)
        return {'batch_id':batch_id,'deduplicated':False}

    def read_batch(self,batch_id):
        from .batch_selection import batch_view
        try:return batch_view(self.root,batch_id)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None

    def change_batch(self,batch_id,status=None,**values):
        with self.guard,session_scope(self.root) as s:
            row=s.get(BatchSelection,batch_id)
            if not row:return False
            data=json.loads(row.payload);data.update(values);row.payload=dump(data)
            if status:row.status=status
            row.updated_at=utcnow();return True

    async def dispatch(self, helper_id):
        if self.closing:
            return
        ws = self.connections.get(helper_id)
        if not ws:
            return
        with self.guard, session_scope(self.root) as s:
            # 等待人工处理同样占用助手，防止别的任务操作同一页面。
            busy = s.scalar(select(CollectorTask).where(CollectorTask.helper_id == helper_id,
                                                       CollectorTask.status.in_(ACTIVE - {"queued"})))
            trend_batches=list(s.scalars(select(TrendDiscoveryBatch).where(
                TrendDiscoveryBatch.helper_id==helper_id,TrendDiscoveryBatch.status.in_({'queued','collecting','waiting_user','supply_search'}))))
            trend_active=False
            for trend_batch in trend_batches:
                state=json.loads(trend_batch.progress_json)
                supply=json.loads(trend_batch.result_json or '{}').get('supply_jobs',[])
                if (any(x.get('status') in {'opening','reading'} for x in state.get('platforms',{}).values()) or
                        any(x.get('status') in {'opening','reading'} for x in supply)):
                    trend_active=True;break
            search_busy=s.scalar(select(BatchSelection).where(BatchSelection.helper_id==helper_id,
                BatchSelection.status.in_({"opening_search","reading_search","waiting_user"})))
            if busy or search_busy or trend_active or any(self.read(t)["helper_id"] == helper_id for t in self.workers):
                return
            trend_batch=next((x for x in trend_batches if x.status in {'queued','collecting','supply_search'}),None)
            if trend_batch:
                progress=json.loads(trend_batch.progress_json);platforms=progress.get('platforms',{})
                platform=next((name for name,state in platforms.items() if state.get('status')=='queued'),None)
                if platform:
                    from .trend_discovery import SEARCH_URLS
                    lease=secrets.token_urlsafe(24);platforms[platform].update(status='opening',lease=lease,
                        dispatched_at=time.time(),message='正在打开'+platform+'搜索页')
                    progress.update(stage='platform_search',message='正在采集'+platform+'爆款候选')
                    trend_batch.status='collecting';trend_batch.progress_json=dump(progress);trend_batch.updated_at=utcnow()
                    template=SEARCH_URLS['douyin_buyin'] if platform=='douyin' and platforms[platform].get('surface')=='buyin' else SEARCH_URLS[platform]
                    command={'type':'trend_search','batch_id':trend_batch.batch_id,'lease':lease,'platform':platform,
                        'category':trend_batch.category,'page_url':template.format(query=quote_from_bytes(trend_batch.category.encode('utf-8'))),
                        'max_candidates':20,'filters':json.loads(trend_batch.filters_json)}
                    task_id=trend_batch.batch_id;batch=None;task=None
                elif trend_batch.status=='supply_search':
                    result=json.loads(trend_batch.result_json or '{}')
                    job=next((x for x in result.get('supply_jobs',[]) if x.get('status')=='queued'),None)
                    if job:
                        lease=secrets.token_urlsafe(24);job.update(status='opening',lease=lease,dispatched_at=time.time())
                        trend_batch.result_json=dump(result);trend_batch.updated_at=utcnow()
                        command={'type':'trend_supply_image' if job['method']=='image' else 'trend_supply_search',
                            'batch_id':trend_batch.batch_id,'job_id':job['job_id'],'trend_id':job['trend_id'],'lease':lease,
                            'page_url':job['page_url'],'keyword':job['keyword'],'max_candidates':30}
                        if job['method']=='image':command['reference_url']=f"http://127.0.0.1:8010/api/trend-discovery/covers/{trend_batch.batch_id}/{job['job_id']}/{job['reference_token']}"
                        task_id=job['job_id'];batch=None;task=None
                    else:command=None;task_id=None;batch=None;task=None
                else:
                    command=None;task_id=None;batch=None;task=None
            else:
                command=None;task_id=None
            batch=s.scalar(select(BatchSelection).where(BatchSelection.helper_id==helper_id,
                BatchSelection.status=='search_queued').order_by(BatchSelection.created_at))
            if trend_batch and command:
                pass
            elif batch:
                data=json.loads(batch.payload);is_image=data.get('search_kind')=='image';data.update(lease=secrets.token_urlsafe(24),dispatched_at=time.time(),
                    phase='opening_search',message='正在打开1688以图搜图页面' if is_image else '正在打开1688搜索页')
                batch.payload=dump(data);batch.status='opening_search';batch.updated_at=utcnow()
                command=dict(type='image_search' if is_image else 'search',batch_id=batch.batch_id,lease=data['lease'],
                             page_url=data.get('search_url','https://s.1688.com/selloffer/offer_search.htm'),
                             keyword=batch.normalized_keyword,max_candidates=batch.max_candidates)
                if is_image:
                    command['reference_url']=f"http://127.0.0.1:8010/api/batch-selection/images/{batch.batch_id}/{data['reference_image']['token']}"
                task_id=batch.batch_id
                task=None
            elif not trend_batch:
                command=None
            task = s.scalar(select(CollectorTask).where(CollectorTask.helper_id == helper_id, CollectorTask.status == "queued")
                            .order_by(CollectorTask.created_at, CollectorTask.task_id))
            if trend_batch and command:
                pass
            elif batch:
                pass
            elif not task:
                return
            else:
                data = json.loads(task.payload)
                batch_id=data.get('batch_id')
                if batch_id:
                    owner=s.get(BatchSelection,batch_id)
                    # SQLAlchemy不对ScalarResult提供count；在Python中计数，候选上限只有60。
                    succeeded=sum(x.status in {'collected','estimating','scored','partial_score','needs_input'} for x in
                        s.scalars(select(BatchSelectionItem).where(BatchSelectionItem.batch_id==batch_id)))
                    if not owner or owner.status not in {'collecting'} or succeeded>=owner.target_count:
                        task.status='cancelled';data.update(message='批量目标已达到，未再打开该候选');task.payload=dump(data);task.updated_at=utcnow()
                        task_id=None;command=None
                    else:task_id=task.task_id
                else:task_id=task.task_id
                if task_id:
                    if data.get("snapshot"):
                        task.status = "normalizing";command = None
                    else:
                        data.update(lease=secrets.token_urlsafe(24), message="请在Chrome/Edge完成可能出现的登录或验证。", dispatched_at=time.time())
                        task.status = "opening"
                        if batch_id:
                            item=s.scalar(select(BatchSelectionItem).where(BatchSelectionItem.batch_id==batch_id,
                                BatchSelectionItem.collector_task_id==task.task_id))
                            if item:item.status='collecting'
                        command = dict(type="collect", task_id=task_id, lease=data["lease"], page_url=task.page_url,
                                       offer_id=task.offer_id,mode=data.get('mode','full'))
                    task.payload = dump(data);task.updated_at = utcnow()
        if task_id is None:
            return
        if command:
            try:
                await ws.send_json(command)
            except (RuntimeError, WebSocketDisconnect):
                self.change(task_id, "interrupted", error_type="helper_disconnected", message=ERRORS["helper_disconnected"])
        else:
            self.start_worker(task_id)

    def owned(self, helper_id, message):
        task_id = str(message.get("task_id", ""))
        with session_scope(self.root) as s:
            task = s.get(CollectorTask, task_id)
            if not task or task.helper_id != helper_id:
                raise ValueError("任务不属于当前助手")
            data = json.loads(task.payload)
            if not secrets.compare_digest(str(message.get("lease", "")), data.get("lease", "!")):
                raise ValueError("调度已失效，请从工作台重新开始")
            return task, data

    def owned_batch(self,helper_id,message):
        batch_id=str(message.get('batch_id',''))
        with session_scope(self.root) as s:
            row=s.get(BatchSelection,batch_id)
            if not row or row.helper_id!=helper_id:raise ValueError('批量任务不属于当前助手')
            data=json.loads(row.payload)
            if not secrets.compare_digest(str(message.get('lease','')),data.get('lease','!')):
                raise ValueError('批量调度已失效')
            return row,data

    async def finish_trend_platform_stage(self,batch_id,helper_id):
        """收口平台采集；即使最后一个平台失败，也继续用已保存证据评分。"""
        from .trend_discovery import finalize_batch
        from .trend_supply import queue_supply_jobs
        result=await asyncio.to_thread(finalize_batch,self.root,batch_id)
        supply_jobs=queue_supply_jobs(self.root,batch_id)
        with self.guard,session_scope(self.root) as s:
            batch=s.get(TrendDiscoveryBatch,batch_id)
            if batch:
                progress=json.loads(batch.progress_json)
                progress.update(scored=result['candidate_count'],qualified=result['qualified_count'])
                if not supply_jobs:progress.update(stage='completed',message='爆款评分完成；没有可执行的平替搜索任务')
                batch.progress_json=dump(progress);batch.updated_at=utcnow()
        await self.dispatch(helper_id)

    async def finish_trend_supply_stage(self,batch_id,helper_id):
        """收口平替检索；失败任务保留原因，成功任务仍进入评分。"""
        from .trend_supply import finalize_supply
        await asyncio.to_thread(finalize_supply,self.root,batch_id)
        await self.dispatch(helper_id)

    async def message(self, helper_id, message):
        if not isinstance(message, dict):
            raise ValueError("消息格式无效")
        action = message.get("type")
        if action in {'listing_progress','listing_result'}:
            task_id=str(message.get('task_id',''))
            with self.guard,session_scope(self.root) as s:
                task=s.get(DistributionTask,task_id)
                if not task or task.helper_id!=helper_id:raise ValueError('铺货任务不属于当前助手')
                values=json.loads(task.payload or '{}')
                if not secrets.compare_digest(str(message.get('lease','')),str(values.get('lease','!'))):
                    raise ValueError('铺货调度已失效')
                if task.status in {'cancelled','completed','failed','interrupted'}:
                    return {'type':action+'_ack','task_id':task_id,'ignored':True}
                if action=='listing_progress':
                    stage=str(message.get('stage',''))
                    if stage not in {'opening','filling','waiting_user'}:raise ValueError('未知铺货阶段')
                    error=str(message.get('error_type') or '')
                    messages={'opening':'正在打开千牛卖家中心','filling':'正在填写可安全识别的草稿字段',
                              'waiting_user':'请在Chrome/Edge完成登录或进入商品发布页后重试'}
                    task.status=stage;values.update(message=messages[stage],error_type=error or None)
                else:
                    filled=message.get('filled') or [];warnings=message.get('warnings') or []
                    if not isinstance(filled,list) or not isinstance(warnings,list) or len(filled)>20 or len(warnings)>20:
                        raise ValueError('铺货结果格式无效')
                    values.update(filled=[str(x)[:100] for x in filled],warnings=[str(x)[:300] for x in warnings],
                        current_url=str(message.get('current_url') or '')[:2000],
                        message='已完成安全字段填写，请核对类目、SKU、价格、库存、运费和资质后由你发布。')
                    task.status='completed'
                task.payload=dump(values);task.updated_at=utcnow()
            return {'type':action+'_ack','task_id':task_id}
        if action in {'trend_supply_progress','trend_supply_snapshot'}:
            batch_id=str(message.get('batch_id',''));job_id=str(message.get('job_id',''))
            with self.guard,session_scope(self.root) as s:
                batch=s.get(TrendDiscoveryBatch,batch_id)
                if not batch or batch.helper_id!=helper_id:raise ValueError('平替任务不属于当前助手')
                result=json.loads(batch.result_json or '{}');job=next((x for x in result.get('supply_jobs',[]) if x.get('job_id')==job_id),None)
                if not job or not secrets.compare_digest(str(message.get('lease','')),str(job.get('lease','!'))):raise ValueError('平替调度已失效')
                if action=='trend_supply_progress':
                    stage=str(message.get('stage',''));error=str(message.get('error_type') or '')
                    if stage not in {'opening','reading','waiting_user','failed'}:raise ValueError('未知平替搜索阶段')
                    job.update(status=stage,error_type=error,message=ERRORS.get(error,'浏览器正在读取1688平替结果。'))
                    if stage in {'waiting_user','failed'}:batch.status='supply_search'
                    batch.result_json=dump(result);batch.updated_at=utcnow()
                    finished=stage=='failed' and all(x.get('status') in {'completed','failed'} for x in result.get('supply_jobs',[]))
                    if finished:asyncio.create_task(self.finish_trend_supply_stage(batch_id,helper_id))
                    elif stage in {'waiting_user','failed'}:asyncio.create_task(self.dispatch(helper_id))
                    return {'type':'trend_supply_progress_ack','batch_id':batch_id,'job_id':job_id}
                try:snapshot=SearchEvidence.model_validate(message.get('snapshot')).model_dump()
                except ValidationError:raise ValueError('平替搜索快照结构无效') from None
                parsed=urlsplit(snapshot['page_url'])
                if parsed.scheme!='https' or parsed.hostname!='s.1688.com':raise ValueError('平替搜索快照来源无效')
                from .trend_supply import save_supply_snapshot
                done=save_supply_snapshot(self.root,batch_id,job_id,snapshot['candidates'])
            if done:
                from .trend_supply import finalize_supply
                await asyncio.to_thread(finalize_supply,self.root,batch_id)
            await self.dispatch(helper_id)
            return {'type':'trend_supply_snapshot_ack','batch_id':batch_id,'job_id':job_id,'completed':done}
        if action in {'trend_search_progress','trend_search_snapshot'}:
            batch_id=str(message.get('batch_id',''));platform=str(message.get('platform',''))
            if platform not in {'taobao','jd','douyin'}:raise ValueError('未知爆款平台')
            with self.guard,session_scope(self.root) as s:
                batch=s.get(TrendDiscoveryBatch,batch_id)
                if not batch or batch.helper_id!=helper_id:raise ValueError('爆款任务不属于当前助手')
                progress=json.loads(batch.progress_json);state=progress.get('platforms',{}).get(platform)
                if not state or not secrets.compare_digest(str(message.get('lease','')),str(state.get('lease','!'))):
                    raise ValueError('爆款调度已失效')
                if action=='trend_search_progress':
                    stage=str(message.get('stage',''))
                    if stage not in {'opening','reading','waiting_user','failed'}:raise ValueError('未知爆款采集阶段')
                    error=str(message.get('error_type') or '')
                    state.update(status=stage,error_type=error,message=ERRORS.get(error,'浏览器正在读取平台可见商品。'),
                        dispatched_at=time.time())
                    if message.get('found') is not None:state['found']=max(0,min(20,int(message.get('found') or 0)))
                    if message.get('detail_completed') is not None:
                        state['detail_completed']=max(0,min(20,int(message.get('detail_completed') or 0)))
                    if stage=='waiting_user' and platform=='douyin' and state.get('surface')=='buyin' and error in {'login_required','verification_required','page_changed'}:
                        state.update(status='queued',surface='public',lease='',message='百应不可用，已自动切换抖音公开商品搜索')
                        batch.status='collecting'
                    elif stage=='waiting_user': batch.status='collecting'
                    batch.progress_json=dump(progress);batch.updated_at=utcnow()
                    finished=stage=='failed' and all(x.get('status') in {'completed','failed'} for x in progress.get('platforms',{}).values())
                    if finished:asyncio.create_task(self.finish_trend_platform_stage(batch_id,helper_id))
                    elif stage in {'waiting_user','failed'}:asyncio.create_task(self.dispatch(helper_id))
                    return {'type':'trend_search_progress_ack','batch_id':batch_id}
                snapshot=message.get('snapshot')
                if not isinstance(snapshot,dict) or snapshot.get('platform')!=platform:raise ValueError('平台快照结构无效')
                records=snapshot.get('candidates')
                if not isinstance(records,list) or len(records)>20:raise ValueError('平台候选超过20条或格式无效')
                if any(not isinstance(x,dict) or len(dump(x))>12000 for x in records):raise ValueError('平台候选证据无效')
                from .trend_discovery import save_sources
                count=save_sources(self.root,batch_id,platform,records)
                detail_count=sum(str(x.get('detail_status','')).startswith('已读取') for x in records)
                state.update(status='completed',found=count,detail_completed=detail_count,
                    message=f'已保存{count}条页面证据，详情补充{detail_count}条',lease='')
                batch.status='collecting';progress['message']='正在继续下一个平台';batch.progress_json=dump(progress);batch.updated_at=utcnow()
                complete=all(x.get('status') in {'completed','failed'} for x in progress.get('platforms',{}).values())
            if complete:
                from .trend_discovery import finalize_batch
                result=finalize_batch(self.root,batch_id)
                from .trend_supply import queue_supply_jobs
                supply_jobs=queue_supply_jobs(self.root,batch_id)
                with session_scope(self.root) as s:
                    batch=s.get(TrendDiscoveryBatch,batch_id);progress=json.loads(batch.progress_json)
                    progress.update(scored=result['candidate_count'],qualified=result['qualified_count'])
                    if not supply_jobs:progress.update(stage='completed',message='爆款评分完成；没有可执行的平替搜索任务')
                    batch.progress_json=dump(progress);batch.updated_at=utcnow()
            await self.dispatch(helper_id)
            return {'type':'trend_search_snapshot_ack','batch_id':batch_id,'found':count,'completed':complete}
        if action == "heartbeat":
            batch_timeouts=[]
            trend_platform_done=[];trend_supply_done=[]
            with session_scope(self.root) as s:
                s.get(CollectorHelper, helper_id).last_seen = utcnow()
                now=time.time()
                # 用户在浏览器完成登录/验证码后，下一次心跳自动重派同一任务。
                for paused in s.scalars(select(CollectorTask).where(
                        CollectorTask.helper_id==helper_id,CollectorTask.status=='waiting_user')):
                    values=json.loads(paused.payload);retries=int(values.get('verification_retry_count',0))
                    if (values.get('error_type') in {'login_required','verification_required'} and
                            now>=float(values.get('auto_retry_at',now+1)) and retries<30):
                        paused.status='queued';values.update(lease='',verification_retry_count=retries+1,
                            message='正在自动检查登录/验证是否已经完成。')
                        paused.payload=dump(values);paused.updated_at=utcnow()
                        batch_id=values.get('batch_id')
                        if batch_id:
                            owner=s.get(BatchSelection,batch_id)
                            if owner:
                                batch_values=json.loads(owner.payload);owner.status='collecting'
                                batch_values.update(phase='collecting',waiting_scope=None,
                                    message='正在自动检查验证并继续当前商品。')
                                owner.payload=dump(batch_values);owner.updated_at=utcnow()
                for paused in s.scalars(select(BatchSelection).where(
                        BatchSelection.helper_id==helper_id,BatchSelection.status=='waiting_user')):
                    values=json.loads(paused.payload);retries=int(values.get('verification_retry_count',0))
                    if (values.get('waiting_scope')=='search' and
                            values.get('error_type') in {'login_required','verification_required'} and
                            now>=float(values.get('auto_retry_at',now+1)) and retries<30):
                        paused.status='search_queued';values.update(phase='search_queued',lease='',
                            verification_retry_count=retries+1,message='正在自动检查验证并重新读取搜索页。')
                        paused.payload=dump(values);paused.updated_at=utcnow()
                for stale in s.scalars(select(CollectorTask).where(CollectorTask.helper_id==helper_id, CollectorTask.status.in_({"opening","reading"}))):
                    values=json.loads(stale.payload)
                    timeout=120 if values.get('batch_id') and values.get('mode')=='text_only' else 300
                    if time.time()-values.get("dispatched_at",time.time()) > timeout:
                        batch_id=values.get('batch_id')
                        if batch_id and values.get('mode')=='text_only':
                            # 批量预筛不能被单个慢页面阻塞；记录失败后继续下一候选。
                            stale.status='failed';batch_timeouts.append((batch_id,stale.task_id))
                            values.update(error_type='capture_timeout',message='页面读取超时，批量任务已跳过并继续下一候选。')
                        else:
                            stale.status="waiting_user"
                            values.update(error_type="capture_timeout",message=ERRORS["capture_timeout"])
                        stale.payload=dump(values)
                        stale.updated_at=utcnow()
                for stale in s.scalars(select(BatchSelection).where(BatchSelection.helper_id==helper_id,
                        BatchSelection.status.in_({'opening_search','reading_search'}))):
                    values=json.loads(stale.payload)
                    if time.time()-values.get('dispatched_at',time.time())>300:
                        stale.status='waiting_user';values.update(phase='waiting_user',error_type='capture_timeout',
                            message='搜索页读取超时，请在Chrome/Edge检查页面后继续。')
                        stale.payload=dump(values);stale.updated_at=utcnow()
                # 平台和1688搜索使用同一串行助手，任何页面卡住都必须释放租约并继续后续任务。
                for batch in s.scalars(select(TrendDiscoveryBatch).where(
                        TrendDiscoveryBatch.helper_id==helper_id,
                        TrendDiscoveryBatch.status.in_({'collecting','supply_search'}))):
                    progress=json.loads(batch.progress_json);result=json.loads(batch.result_json or '{}');changed=False
                    for state in progress.get('platforms',{}).values():
                        if state.get('status') in {'opening','reading'} and now-float(state.get('dispatched_at',now))>180:
                            state.update(status='failed',lease='',error_type='capture_timeout',
                                message='平台页面读取超时，已跳过并继续其他平台。');changed=True
                    for job in result.get('supply_jobs',[]):
                        if job.get('status') in {'opening','reading'} and now-float(job.get('dispatched_at',now))>180:
                            job.update(status='failed',lease='',error_type='capture_timeout',
                                message='1688搜索页读取超时，已跳过并继续其他任务。');changed=True
                    if changed:
                        batch.progress_json=dump(progress);batch.result_json=dump(result);batch.updated_at=utcnow()
                    platforms=progress.get('platforms',{})
                    if platforms and all(x.get('status') in {'completed','failed'} for x in platforms.values()):
                        if not result.get('supply_jobs') and batch.status=='collecting':trend_platform_done.append(batch.batch_id)
                    jobs=result.get('supply_jobs',[])
                    if jobs and all(x.get('status') in {'completed','failed'} for x in jobs):trend_supply_done.append(batch.batch_id)
            for batch_id,task_id in batch_timeouts:
                await self.finish_batch_item(batch_id,task_id,None,None,'capture_timeout')
            for batch_id in dict.fromkeys(trend_platform_done):
                await self.finish_trend_platform_stage(batch_id,helper_id)
            for batch_id in dict.fromkeys(trend_supply_done):
                await self.finish_trend_supply_stage(batch_id,helper_id)
            await self.dispatch(helper_id)
            return {"type": "heartbeat"}
        if action in {'search_progress','search_snapshot'}:
            with self.guard:
                batch,data=self.owned_batch(helper_id,message)
                if action=='search_progress':
                    stage=message.get('stage')
                    if stage not in {'opening_search','reading_search','waiting_user'}:raise ValueError('未知搜索阶段')
                    error=message.get('error_type')
                    self.change_batch(batch.batch_id,stage,phase=stage,error_type=error,
                        waiting_scope='search' if stage=='waiting_user' else None,
                        auto_retry_at=time.time()+8 if stage=='waiting_user' else None,
                        message=ERRORS.get(error,'浏览器正在读取1688以图搜图结果。' if data.get('search_kind')=='image' else '浏览器正在读取1688搜索结果。'))
                    return {'type':'search_progress_ack','batch_id':batch.batch_id}
                try:snapshot=SearchEvidence.model_validate(message.get('snapshot')).model_dump()
                except ValidationError:raise ValueError('搜索快照结构无效') from None
                parsed=urlsplit(snapshot['page_url'])
                if parsed.scheme!='https' or parsed.hostname!='s.1688.com':raise ValueError('搜索快照来源无效')
                if batch.status not in {'opening_search','reading_search'}:raise ValueError('当前阶段不接受搜索快照')
                from .batch_selection import filter_candidates
                chosen=filter_candidates(snapshot['candidates'],json.loads(batch.filters_json),batch.max_candidates)
                digest=hashlib.sha256(dump(snapshot).encode()).hexdigest()
                if data.get('snapshot_hash'):
                    if data['snapshot_hash']!=digest:raise ValueError('已接收另一份搜索快照')
                    return {'type':'search_snapshot_ack','batch_id':batch.batch_id,'duplicate':True}
                now=utcnow()
                for item in chosen:
                    task_id=f"CT-{item['relevance_rank']:03d}-"+uuid.uuid4().hex
                    s_item=BatchSelectionItem(batch_id=batch.batch_id,offer_id=item['offer_id'],
                        relevance_rank=item['relevance_rank'],title=item['title'],page_url=item['page_url'],
                        status='queued',collector_task_id=task_id,payload=dump({'card':item['card']}))
                    with session_scope(self.root) as s:
                        s.add(s_item)
                        s.add(CollectorTask(task_id=task_id,helper_id=helper_id,offer_id=item['offer_id'],
                            page_url=item['page_url'],status='queued',payload=dump({'found':0,'downloaded':0,'failed':0,
                            'image_results':[],'attempt':1,'mode':'text_only','batch_id':batch.batch_id,
                            'relevance_rank':item['relevance_rank']}),created_at=now,updated_at=now))
                status='collecting' if chosen else 'failed'
                self.change_batch(batch.batch_id,status,phase=status,found=len(chosen),snapshot_hash=digest,
                    diagnostics=snapshot['diagnostics'],message=('已找到候选，正在逐个读取文字资料。' if chosen else
                    '搜索页没有识别到有效商品，请检查登录、验证或搜索结果。'))
            await self.dispatch(helper_id)
            return {'type':'search_snapshot_ack','batch_id':batch.batch_id,'duplicate':False,'found':len(chosen)}
        with self.guard:
            task, data = self.owned(helper_id, message)
            if action == "snapshot":
                try:
                    snapshot = PageEvidence.model_validate(message.get("snapshot")).model_dump()
                except ValidationError:
                    raise ValueError("快照包含未知字段或超过长度限制；不会保存") from None
                if any(len(x) > 1000 for x in snapshot["diagnostics"]):
                    raise ValueError("区域诊断过长")
                if validate_1688_offer_url(snapshot["page_url"]) != task.offer_id:
                    raise ValueError("实际商品ID与请求不一致")
                if len({r["region"] for r in snapshot["regions"]}) != len(snapshot["regions"]):
                    raise ValueError("区域诊断重复")
                for region in snapshot['regions']:
                    region['found']=len({i['url'] for i in snapshot['images'] if i['region']==region['region']})
                    region['complete']=region['complete'] and region['recognized'] and region['found']>0
                digest = hashlib.sha256(dump(snapshot).encode()).hexdigest()
                if data.get("snapshot_hash"):
                    if data["snapshot_hash"] != digest:
                        raise ValueError("本次任务已接收另一份快照，不允许覆盖")
                    return {"type": "snapshot_ack", "task_id":task.task_id, "duplicate":True}
                if task.status not in {"opening", "reading"}:
                    raise ValueError("当前阶段不接受快照")
                if not snapshot["title"] and not snapshot["visible_text"]:
                    self.change(task.task_id, "failed", error_type="empty_page", message="未读取到标题和商品资料，不能确认成功。")
                    # 空页面不会进入后台 process，因此必须显式结束批次项并继续派发；
                    # 否则一个受登录页/结构变化影响的候选会卡住整个串行批次。
                    asyncio.create_task(self.continue_after_empty_snapshot(
                        helper_id, data.get("batch_id"), task.task_id
                    ))
                    return {"type":"snapshot_ack", "task_id":task.task_id, "failed":True}
                self.change(task.task_id, "normalizing", snapshot=snapshot, snapshot_hash=digest,
                            found=len({x["url"] for x in snapshot["images"]}), regions=snapshot["regions"],
                            diagnostics=snapshot["diagnostics"], message="页面证据已保存，正在整理与下载。")
                self.start_worker(task.task_id)
                return {"type":"snapshot_ack", "task_id":task.task_id, "duplicate":False}
            if action == "progress":
                stage = message.get("stage")
                if task.status not in {"opening", "reading"} or stage not in {"opening", "reading", "waiting_user"}:
                    raise ValueError("不允许的阶段转换")
                if task.status == "reading" and stage == "opening":
                    raise ValueError("不能倒退到打开页面阶段")
                error = message.get("error_type")
                if stage == "waiting_user" and error not in ERRORS:
                    raise ValueError("未知人工处理原因")
                self.change(task.task_id, stage, error_type=error if stage == "waiting_user" else None,
                            auto_retry_at=time.time()+8 if stage=='waiting_user' else None,
                            message=ERRORS.get(error, "浏览器正在读取商品区域和图片。"))
                if data.get('batch_id') and stage=='waiting_user':
                    self.change_batch(data['batch_id'],'waiting_user',phase='waiting_user',error_type=error,
                        waiting_scope='item',message=ERRORS.get(error,'请在Chrome/Edge处理当前商品页后继续。'))
                return {"type":"progress_ack", "task_id":task.task_id}
            raise ValueError("不支持的采集动作")

    async def continue_after_empty_snapshot(self, helper_id, batch_id, task_id):
        if batch_id:
            await self.finish_batch_item(batch_id, task_id, None, None, "empty_page")
        await self.dispatch(helper_id)

    def start_worker(self, task_id):
        if task_id not in self.workers:
            self.workers[task_id] = asyncio.create_task(self.process(task_id))

    def normalize(self, snapshot):
        """模型只读，不能入库：超时后后台线程返回也不会污染已结束任务。"""
        from .agents import _json, _make_agent, _skill_trace, _text
        agent, model = _make_agent(self.root, "collector", "只整理页面证据，非空facts必须逐字出现在证据中，缺失留空。输出facts JSON，不浏览网页。")
        response = agent.invoke({"messages":[{"role":"user", "content":dump({"title":snapshot["title"],
            "visible_text":snapshot["visible_text"], "text_blocks":snapshot.get('text_blocks',[]), "allowed_fields":FIELDS})}]}, {"recursion_limit":6})
        _skill_trace(response, "data-collector")
        result = _json(_text(response))
        facts = result.get("facts") if isinstance(result, dict) else None
        if not isinstance(facts, dict):
            raise ValueError("模型字段结构无效")
        evidence = snapshot["title"] + snapshot["visible_text"] + '\n'.join(b['text'] for b in snapshot.get('text_blocks',[]))
        clean = {}
        for key in FIELDS:
            value = facts.get(key)
            if value:
                if not isinstance(value, str) or len(value) > 500 or value not in evidence:
                    raise ValueError("模型字段缺少原文证据")
                clean[key] = value
        return clean, model

    async def enrich(self, task_id, run_id, snapshot):
        if not self.model_enabled:
            return
        try:
            facts, model = await asyncio.wait_for(asyncio.to_thread(self.normalize, snapshot), timeout=95)
            if self.read(task_id)["status"] in TERMINAL:
                return
            # 保留确定性字段，冲突只记录、不覆盖，不改任何选品分数。
            with session_scope(self.root) as s:
                run = s.get(CollectionRun, run_id)
                normalized = json.loads(run.normalized_json)
                conflicts = list(normalized.get('conflicts',[]))
                for field, value in facts.items():
                    previous = normalized["facts"].get(field)
                    if previous and previous != value:
                        conflicts.append(dict(field=field, values=[previous, value], status="待人工确认"))
                    elif not previous and field!='title':
                        normalized["facts"][field] = value
                        s.add(CollectedFact(fact_id="CF-"+uuid.uuid4().hex, run_id=run_id, product_id=run.product_id,
                                           field_name=field, raw_value=value, normalized_value=value,
                                           source_locator="page.visible_text", confidence=.75, verification_status="待复核"))
                normalized["conflicts"] = conflicts
                run.normalized_json = dump(normalized)
                run.model = model
            self.change(task_id, model_status="completed", conflicts=conflicts)
        except Exception as exc:
            # 不打印SDK异常：异常可能携带完整URL/请求头。
            self.change(task_id, model_status="fallback", model_error_type=type(exc).__name__)

    async def finish_batch_item(self,batch_id,task_id,run_id,product_id,error_type=None):
        """记录单商品结果，并在达到目标或候选耗尽时启动统一估价。"""
        launch=False
        with self.guard,session_scope(self.root) as s:
            batch=s.get(BatchSelection,batch_id)
            item=s.scalar(select(BatchSelectionItem).where(BatchSelectionItem.batch_id==batch_id,
                BatchSelectionItem.collector_task_id==task_id))
            if not batch or not item:return
            if error_type:
                item.status='filtered' if error_type=='filtered_out' else 'failed';item.error_type=error_type
            else:
                item.status='collected';item.product_id=product_id
                values=json.loads(item.payload);values['run_id']=run_id;item.payload=dump(values)
            items=list(s.scalars(select(BatchSelectionItem).where(BatchSelectionItem.batch_id==batch_id)))
            successes=sum(x.status in {'collected','estimating','scored','partial_score','needs_input'} for x in items)
            pending=[x for x in items if x.status in {'queued','collecting'}]
            if successes>=batch.target_count:
                for extra in pending:
                    extra.status='skipped'
                    if extra.collector_task_id:
                        task=s.get(CollectorTask,extra.collector_task_id)
                        if task and task.status=='queued':task.status='cancelled'
                pending=[]
            data=json.loads(batch.payload);data.update(phase='collecting',collected=successes,
                message=f'已完成 {successes}/{batch.target_count} 个商品文字采集。')
            batch.payload=dump(data);batch.updated_at=utcnow()
            active_tasks=any((s.get(CollectorTask,x.collector_task_id) and
                s.get(CollectorTask,x.collector_task_id).status in ACTIVE) for x in pending if x.collector_task_id)
            if successes and (successes>=batch.target_count or not active_tasks):
                batch.status='estimating';data.update(phase='estimating',message='文字采集结束，正在生成逐SKU参考价和供应商代理分。')
                batch.payload=dump(data);launch=True
            elif not successes and not active_tasks:
                batch.status='failed';data.update(phase='failed',message='候选详情均未能结构化，未写入稳定池。')
                batch.payload=dump(data)
        if launch and batch_id not in self.batch_workers:
            self.batch_workers[batch_id]=asyncio.create_task(self.finalize_batch(batch_id))

    async def finalize_batch(self,batch_id):
        """最多两个模型调用并发；所有商品结束后只统一计算一次排名。"""
        try:
            with session_scope(self.root) as s:
                batch=s.get(BatchSelection,batch_id)
                items=[x for x in s.scalars(select(BatchSelectionItem).where(
                    BatchSelectionItem.batch_id==batch_id).order_by(BatchSelectionItem.relevance_rank))
                    if x.status=='collected'][:batch.target_count]
            semaphore=asyncio.Semaphore(2)
            async def estimate(item):
                with self.guard,session_scope(self.root) as s:
                    row=s.get(BatchSelectionItem,(batch_id,item.offer_id));row.status='estimating'
                try:
                    from .stable_estimation import estimate_stable
                    async with semaphore:await asyncio.to_thread(estimate_stable,self.root,item.product_id,False,False)
                    with self.guard,session_scope(self.root) as s:s.get(BatchSelectionItem,(batch_id,item.offer_id)).status='scored'
                except Exception as exc:
                    with self.guard,session_scope(self.root) as s:
                        row=s.get(BatchSelectionItem,(batch_id,item.offer_id));row.status='partial_score';row.error_type=type(exc).__name__
            await asyncio.gather(*(estimate(x) for x in items))
            from .stable import score_stable_products
            await asyncio.to_thread(score_stable_products,self.root)
            with self.guard,session_scope(self.root) as s:
                for item in items:
                    row=s.get(BatchSelectionItem,(batch_id,item.offer_id))
                    candidate=s.get(BusinessRow,('candidate',item.product_id))
                    promoted=s.get(BusinessRow,('stable',item.product_id))
                    if promoted and not candidate:
                        row.status='scored';row.error_type=None
                    else:
                        row.status='needs_input';row.error_type='incomplete_score'
            view=self.read_batch(batch_id)
            scoring_failures=sum(x['status'] in {'partial_score','needs_input'} for x in view['items'])
            status='completed' if view['collected']>=view['target_count'] and scoring_failures==0 else 'partial'
            self.change_batch(batch_id,status,phase=status,
                message=f"批量评分结束：完整 {view['scored']} 个，评分需重试 {scoring_failures} 个。")
        except Exception as exc:
            self.change_batch(batch_id,'partial',phase='partial',error_type=type(exc).__name__,
                message='已采集商品保留；统一评分未完成，可重试失败项。')
        finally:self.batch_workers.pop(batch_id,None)

    async def process(self, task_id):
        enrichment = None
        try:
            with session_scope(self.root) as s:
                data = json.loads(s.get(CollectorTask, task_id).payload)
            snapshot = data["snapshot"]
            run_id = data.get("run_id")
            if not run_id:
                # 使用任务派生的运行ID，进程在入库后崩溃也不会重复创建采集记录。
                result = await asyncio.to_thread(collect_snapshot, self.root, snapshot, False, task_id.replace('CT-','CR-',1))
                run_id = result["run_id"]
                # 即使恰好取消也保留已写入的运行ID，便于追溯而不假称完成。
                with self.guard, session_scope(self.root) as s:
                    task = s.get(CollectorTask, task_id)
                    payload = json.loads(task.payload)
                    payload["run_id"] = run_id
                    task.payload = dump(payload)
            if self.read(task_id)["status"] in TERMINAL:
                return
            mode=data.get('mode','full');batch_id=data.get('batch_id')
            if mode=='text_only':
                try:
                    from .batch_selection import detail_filter
                    if batch_id:
                        with session_scope(self.root) as s:filters=json.loads(s.get(BatchSelection,batch_id).filters_json)
                        accepted,reason=detail_filter(self.root,run_id,filters)
                        if not accepted:
                            self.change(task_id,'completed',model_status='rules',found=len(snapshot.get('images',[])),downloaded=0,
                                failed=0,message='文字已保存，但未通过详情过滤：'+reason)
                            await self.finish_batch_item(batch_id,task_id,run_id,None,'filtered_out');return
                    from .collector import auto_enroll_run
                    enrolled=await asyncio.to_thread(auto_enroll_run,self.root,run_id)
                    from .workbench import capture_cover
                    try:
                        await asyncio.to_thread(capture_cover,self.root,enrolled['product_id'],snapshot)
                    except Exception:
                        pass  # A list-only cover must never block text intake or scoring.
                    self.change(task_id,'completed',model_status='rules',found=len(snapshot.get('images',[])),
                        downloaded=0,failed=0,message='文字与SKU已保存，已尝试保存一张列表封面；完整素材将在选品后采集。')
                    if batch_id:await self.finish_batch_item(batch_id,task_id,run_id,enrolled['product_id'])
                except Exception as exc:
                    self.change(task_id,'failed',error_type=type(exc).__name__,message='文字资料不满足自动入池条件。')
                    if batch_id:await self.finish_batch_item(batch_id,task_id,run_id,None,type(exc).__name__)
                return
            enrichment = asyncio.create_task(self.enrich(task_id, run_id, snapshot))
            with session_scope(self.root) as s:
                saved = json.loads(s.get(CollectionRun, run_id).raw_snapshot_json)
            ids = saved.get("asset_ids", [])
            results = data.get("image_results", [])
            successes = {r["requested_asset_id"] for r in results if r.get("success")}
            results = [r for r in results if r.get("success")]
            self.change(task_id, "downloading", model_status="running" if self.model_enabled else "rules",
                        found=len(ids), image_results=results, downloaded=len(results), failed=0)
            for aid in ids:
                if self.read(task_id)["status"] in TERMINAL:
                    return
                if aid in successes:
                    continue
                try:
                    outcome = await asyncio.to_thread(download_asset, self.root, aid)
                    success = any(x.get("status") == "success" for x in outcome.get("attempts", []))
                    with session_scope(self.root) as s:
                        asset = resolve(s, s.get(ProductAsset, aid))
                        row = dict(requested_asset_id=aid, asset_id=asset.asset_id, success=success,
                                   retained_history=bool(asset.local_path and not success),
                                   status=asset.status, width=asset.width, height=asset.height,
                                   attempts=outcome.get("attempts", []))
                except Exception as exc:
                    row = dict(requested_asset_id=aid, asset_id=aid, success=False, error_type=type(exc).__name__)
                results.append(row)
                # 取消时也保留已完成的单张文件结果；不再下载下一张。
                with self.guard, session_scope(self.root) as s:
                    task = s.get(CollectorTask, task_id)
                    payload = json.loads(task.payload)
                    payload.update(image_results=results, downloaded=sum(r["success"] for r in results),
                                   failed=sum(not r["success"] for r in results))
                    task.payload = dump(payload)
                    task.updated_at = utcnow()
            await enrichment
            with session_scope(self.root) as s:
                captured=s.get(CollectionRun,run_id)
                image_product=captured.product_id if captured else None
            # 页面真实字段归一化完成后，自动为仍缺失且允许推断的字段生成
            # “AI推测”复核草稿。模型不得覆盖页面事实，失败也不丢失已下载图片。
            if image_product and self.model_enabled and self.read(task_id)['status'] not in TERMINAL:
                try:
                    from .collector_enrichment import run_collection_enrichment
                    completion=await asyncio.wait_for(
                        asyncio.to_thread(run_collection_enrichment,self.root,run_id),timeout=150)
                    with self.guard,session_scope(self.root) as s:
                        task=s.get(CollectorTask,task_id);payload=json.loads(task.payload)
                        payload['fact_completion']=completion;task.payload=dump(payload)
                except Exception as exc:
                    with self.guard,session_scope(self.root) as s:
                        task=s.get(CollectorTask,task_id);payload=json.loads(task.payload)
                        payload['fact_completion']={'status':'failed','error_type':type(exc).__name__}
                        task.payload=dump(payload)
            # 图片分类覆盖已有网页类别；下载成功之后再OCR，逐张失败可复核重试。
            from .image_workflow import classify_product
            if image_product and self.read(task_id)['status'] not in TERMINAL:
                self.change(task_id,'normalizing',message='正在逐张OCR与图片语义分类；原图已保存。')
                classification=await asyncio.to_thread(classify_product,self.root,image_product)
                with self.guard, session_scope(self.root) as s:
                    task=s.get(CollectorTask,task_id);payload=json.loads(task.payload)
                    payload['classification']=classification;task.payload=dump(payload)
                if self.read(task_id)['status'] in TERMINAL:return
            completed_regions={r["region"] for r in snapshot["regions"] if r["recognized"] and r["complete"]}
            missing = not {"主图", "SKU图", "详情图"}.issubset(completed_regions)
            partial = not ids or missing or any(not r["success"] for r in results) or (image_product and classification['failed']>0)
            self.change(task_id, "partial" if partial else "completed",
                        message="文字已保存，未发现图片；请查看区域诊断并重试。" if not ids else
                        "资料与本次下载已保存；部分区域或图片不完整，请复核。" if partial else
                        "本次采集与图片下载完成；资料待复核，图片使用权需另行确认。")
        except Exception as exc:
            self.change(task_id, "failed", error_type=type(exc).__name__, message="资料处理失败；已保存的快照和图片保留，可重试。")
            try:
                if data.get('batch_id'):
                    await self.finish_batch_item(data['batch_id'],task_id,data.get('run_id'),None,type(exc).__name__)
            except (UnboundLocalError,AttributeError):
                pass
        finally:
            if enrichment and not enrichment.done():
                enrichment.cancel()
            self.workers.pop(task_id, None)
            await self.dispatch(self.read(task_id)["helper_id"])

    async def resume(self, task_id):
        with self.guard, session_scope(self.root) as s:
            task = s.get(CollectorTask, task_id)
            if not task:
                raise HTTPException(404, "任务不存在")
            if task.status not in {"waiting_user", "interrupted", "failed", "cancelled"}:
                raise HTTPException(409, "当前任务无需继续；部分完成可重新提交链接采集缺失区域")
            if task.helper_id not in self.connections:
                raise HTTPException(409, "原采集助手未连接，请先重新连接")
            if task_id in self.workers:
                raise HTTPException(409, "正在收尾当前图片，请稍后重试")
            other = s.scalar(select(CollectorTask).where(CollectorTask.offer_id == task.offer_id,
                                                         CollectorTask.task_id != task_id, CollectorTask.status.in_(ACTIVE)))
            if other:
                raise HTTPException(409, "同一商品已有活动任务")
            data = json.loads(task.payload)
            data.update(attempt=data.get("attempt", 1)+1, error_type=None, message="已显式继续任务", lease="")
            task.payload = dump(data)
            task.status = "queued"
            task.updated_at = utcnow()
        await self.dispatch(task.helper_id)
        return self.read(task_id)

    async def cancel(self, task_id):
        item = self.read(task_id)
        self.change(task_id, "cancelled", message="用户已取消；正在下载的单张处理结束后停止，已有资料保留。")
        ws = self.connections.get(item["helper_id"])
        if ws:
            try:
                await ws.send_json(dict(type="cancel", task_id=task_id))
            except (RuntimeError, WebSocketDisconnect):
                pass
        await self.dispatch(item["helper_id"])
        return self.read(task_id)

    async def resume_batch(self,batch_id):
        # 过滤规则升级后直接复用已经保存的文字快照，不要求浏览器把同一批商品
        # 再打开一遍。只恢复目标数量，剩余候选继续保留为历史记录。
        reconsider=[]
        with self.guard,session_scope(self.root) as s:
            existing=s.get(BatchSelection,batch_id)
            if existing:
                rows=list(s.scalars(select(BatchSelectionItem).where(
                    BatchSelectionItem.batch_id==batch_id).order_by(BatchSelectionItem.relevance_rank)))
                for row in rows:
                    if len(reconsider)>=existing.target_count:break
                    if row.status=='filtered' and row.error_type=='filtered_out' and row.collector_task_id:
                        task=s.get(CollectorTask,row.collector_task_id)
                        values=json.loads(task.payload) if task else {}
                        if values.get('run_id'):reconsider.append((row.offer_id,values['run_id']))
        if reconsider:
            from .batch_selection import detail_filter
            from .collector import auto_enroll_run
            with session_scope(self.root) as s:
                filters=json.loads(s.get(BatchSelection,batch_id).filters_json)
            for offer_id,run_id in reconsider:
                try:
                    accepted,_=detail_filter(self.root,run_id,filters)
                    if not accepted:continue
                    enrolled=await asyncio.to_thread(auto_enroll_run,self.root,run_id)
                    with self.guard,session_scope(self.root) as s:
                        row=s.get(BatchSelectionItem,(batch_id,offer_id))
                        row.status='collected';row.product_id=enrolled['product_id'];row.error_type=None
                        values=json.loads(row.payload);values['run_id']=run_id;row.payload=dump(values)
                except Exception:
                    # 单个历史快照不可恢复时仍可走下面的常规失败重试流程。
                    continue
        with self.guard,session_scope(self.root) as s:
            batch=s.get(BatchSelection,batch_id)
            if not batch:raise HTTPException(404,'批量任务不存在')
            if batch.helper_id not in self.connections:raise HTTPException(409,'原采集助手未连接')
            data=json.loads(batch.payload)
            items=list(s.scalars(select(BatchSelectionItem).where(BatchSelectionItem.batch_id==batch_id)))
            if not items:
                # 必须删除上次搜索快照指纹；否则相同页面的重试会被当作重复消息，
                # 批次将永久停在 reading_search。
                for key in ('snapshot_hash','diagnostics','dispatched_at','error_type'):
                    data.pop(key,None)
                batch.status='search_queued';data.update(phase='search_queued',message='正在重新打开并读取搜索页',
                    lease='',found=0)
            else:
                for item in items:
                    if item.status=='partial_score':item.status='collected';item.error_type=None
                    if item.collector_task_id:
                        task=s.get(CollectorTask,item.collector_task_id)
                        if task and (item.status=='failed' or task.status in {'waiting_user','interrupted','failed'}):
                            task.status='queued';payload=json.loads(task.payload);payload.pop('snapshot',None);payload.pop('snapshot_hash',None)
                            payload.update(lease='',attempt=payload.get('attempt',1)+1);task.payload=dump(payload);task.updated_at=utcnow()
                            item.status='queued';item.error_type=None
                collected=any(x.status=='collected' for x in items)
                queued=any(x.status=='queued' for x in items)
                batch.status='collecting' if queued else 'estimating'
                data.update(phase=batch.status,message='正在重试失败商品。' if queued else '正在重试AI评分。')
            batch.payload=dump(data);batch.updated_at=utcnow();status=batch.status;helper=batch.helper_id
        if status=='estimating' and batch_id not in self.batch_workers:
            self.batch_workers[batch_id]=asyncio.create_task(self.finalize_batch(batch_id))
        else:await self.dispatch(helper)
        return self.read_batch(batch_id)

    async def cancel_batch(self,batch_id):
        with self.guard,session_scope(self.root) as s:
            batch=s.get(BatchSelection,batch_id)
            if not batch:raise HTTPException(404,'批量任务不存在')
            was_search=batch.status in {'search_queued','opening_search','reading_search','waiting_user'}
            batch.status='cancelled';data=json.loads(batch.payload);data.update(phase='cancelled',message='用户已取消；已保存商品保留。')
            batch.payload=dump(data);batch.updated_at=utcnow();helper=batch.helper_id
            active=[]
            for item in s.scalars(select(BatchSelectionItem).where(BatchSelectionItem.batch_id==batch_id)):
                task=s.get(CollectorTask,item.collector_task_id) if item.collector_task_id else None
                if task and task.status in ACTIVE:
                    active.append(task.task_id);task.status='cancelled';item.status='skipped'
        ws=self.connections.get(helper)
        if ws:
            if was_search:active.insert(0,batch_id)
            for task_id in active:
                try:await ws.send_json({'type':'cancel','task_id':task_id})
                except (RuntimeError,WebSocketDisconnect):break
        await self.dispatch(helper)
        return self.read_batch(batch_id)

    async def submit_assets_for_products(self,product_ids):
        from .database import Product
        if not isinstance(product_ids,list) or not 1<=len(product_ids)<=20:raise HTTPException(422,'请选择1到20个商品')
        results=[]
        for product_id in dict.fromkeys(str(x) for x in product_ids):
            with session_scope(self.root) as s:
                product=s.get(Product,product_id)
                if not product or not product.source_product_url:raise HTTPException(404,f'商品不存在或缺少详情链接：{product_id}')
                url=product.source_product_url
            result=await self.submit(url,'assets');results.append({'product_id':product_id,**result})
        return {'items':results}

    def read_distribution(self, task_id):
        with session_scope(self.root) as s:
            task=s.get(DistributionTask,task_id)
            if not task:raise HTTPException(404,'铺货任务不存在')
            values=json.loads(task.payload or '{}')
            return {'task_id':task.task_id,'package_id':task.package_id,'platform':task.platform,
                'status':task.status,'message':values.get('message',''),'filled':values.get('filled',[]),
                'warnings':values.get('warnings',[]),'current_url':values.get('current_url'),
                'error_type':values.get('error_type'),'created_at':task.created_at,'updated_at':task.updated_at}

    async def submit_distribution(self, package_id, listing):
        """把已确认内容包交给扩展；命令只允许打开后台并填草稿文本。"""
        if listing.get('platform')!='淘宝':raise HTTPException(422,'当前只支持淘宝辅助填写')
        title=str((listing.get('autofill') or {}).get('title') or '')[:120]
        description=str((listing.get('autofill') or {}).get('description') or '')[:12000]
        if not title:raise HTTPException(409,'上架包缺少商品标题')
        with self.guard,session_scope(self.root) as s:
            if not s.get(ListingPackage,package_id):raise HTTPException(404,'内容包不存在')
            helpers=[hid for hid in self.connections if s.get(CollectorHelper,hid).version==VERSION]
            if not helpers:raise HTTPException(409,f'铺货辅助要求采集助手{VERSION}，请更新扩展并重新连接')
            helper_id=sorted(helpers)[0]
            collector_busy=s.scalar(select(CollectorTask).where(CollectorTask.helper_id==helper_id,CollectorTask.status.in_(ACTIVE)))
            listing_busy=s.scalar(select(DistributionTask).where(DistributionTask.helper_id==helper_id,
                DistributionTask.status.in_({'opening','filling','waiting_user'})))
            if collector_busy or listing_busy:raise HTTPException(409,'浏览器助手正在执行其他任务，请完成或取消后再试')
            task_id='DT-'+uuid.uuid4().hex;lease=secrets.token_urlsafe(24);now=utcnow()
            values={'lease':lease,'message':'正在打开千牛卖家中心','filled':[],'warnings':[],
                'dispatched_at':time.time()}
            s.add(DistributionTask(task_id=task_id,package_id=package_id,helper_id=helper_id,
                platform='淘宝',status='opening',payload=dump(values),created_at=now,updated_at=now))
        command={'type':'listing_fill','task_id':task_id,'package_id':package_id,'lease':lease,
            'platform':'淘宝','page_url':listing['seller_url'],'fields':{'title':title,'description':description}}
        try:await self.connections[helper_id].send_json(command)
        except (RuntimeError,WebSocketDisconnect):
            with session_scope(self.root) as s:
                row=s.get(DistributionTask,task_id);values=json.loads(row.payload);values.update(error_type='helper_disconnected',message='助手连接中断，请重试')
                row.status='interrupted';row.payload=dump(values);row.updated_at=utcnow()
        return self.read_distribution(task_id)

    async def cancel_distribution(self,task_id):
        with self.guard,session_scope(self.root) as s:
            row=s.get(DistributionTask,task_id)
            if not row:raise HTTPException(404,'铺货任务不存在')
            if row.status in {'completed','failed','cancelled','interrupted'}:return self.read_distribution(task_id)
            values=json.loads(row.payload);values['message']='已取消辅助填写，卖家后台页面保持打开。'
            row.status='cancelled';row.payload=dump(values);row.updated_at=utcnow();helper=row.helper_id
        ws=self.connections.get(helper)
        if ws:
            try:await ws.send_json({'type':'cancel','task_id':task_id})
            except (RuntimeError,WebSocketDisconnect):pass
        return self.read_distribution(task_id)

    async def websocket(self, ws):
        origin = ws.headers.get("origin", "")
        if not extension_origin(origin):
            await ws.close(code=1008)
            return
        await ws.accept()
        helper_id = None
        try:
            raw = await asyncio.wait_for(ws.receive_text(), timeout=10)
            if len(raw) > 2000:
                raise ValueError("认证消息过长")
            auth = json.loads(raw)
            with session_scope(self.root) as s:
                h = s.get(CollectorHelper, str(auth.get("helper_id", "")))
                if (not h or auth.get("type") != "hello" or auth.get("protocol") != PROTOCOL
                        or h.extension_origin != origin or auth.get("version") not in COMPATIBLE_VERSIONS
                        or not secrets.compare_digest(h.token_hash, hashlib.sha256(str(auth.get("token", "")).encode()).hexdigest())):
                    raise ValueError("认证失败")
                helper_id = h.helper_id
                if helper_id in self.connections:
                    raise ValueError("助手已有连接")
                h.connected = True
                h.version = auth['version']
                h.last_seen = utcnow()
            self.connections[helper_id] = ws
            await ws.send_json(dict(type="hello_ack", protocol=PROTOCOL))
            await self.dispatch(helper_id)
            while True:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=50)
                if len(raw) > 1_500_000:
                    raise ValueError("快照过大")
                try:
                    reply = await self.message(helper_id, json.loads(raw))
                    await ws.send_json(reply)
                except (ValueError, ValidationError, TypeError):
                    # 不能回显不受信任的消息或异常内容。
                    await ws.send_json(dict(type="rejected", message="任务归属、商品ID、阶段或快照格式校验失败，请检查后重试。"))
        except (ValueError, TypeError, AttributeError, asyncio.TimeoutError, WebSocketDisconnect):
            try:
                await ws.close(code=1008)
            except (RuntimeError, WebSocketDisconnect):
                pass
        finally:
            if helper_id and self.connections.get(helper_id) is ws:
                self.connections.pop(helper_id, None)
                with self.guard, session_scope(self.root) as s:
                    s.get(CollectorHelper, helper_id).connected = False
                    for task in s.scalars(select(CollectorTask).where(CollectorTask.helper_id == helper_id,
                                                                     CollectorTask.status.in_({"queued", "opening", "reading", "waiting_user"}))):
                        data = json.loads(task.payload)
                        data.update(error_type="helper_disconnected", message=ERRORS["helper_disconnected"])
                        task.status = "interrupted"
                        task.payload = dump(data)
                    for batch in s.scalars(select(BatchSelection).where(BatchSelection.helper_id==helper_id,
                            BatchSelection.status.in_({'search_queued','opening_search','reading_search','waiting_user'}))):
                        data=json.loads(batch.payload);data.update(phase='interrupted',error_type='helper_disconnected',
                            message='助手连接中断；已保存候选保留，可继续任务。')
                        batch.status='interrupted';batch.payload=dump(data);batch.updated_at=utcnow()
                    for task in s.scalars(select(DistributionTask).where(DistributionTask.helper_id==helper_id,
                            DistributionTask.status.in_({'opening','filling','waiting_user'}))):
                        values=json.loads(task.payload or '{}');values.update(error_type='helper_disconnected',
                            message='助手连接中断；卖家后台不会自动发布，请重新发起辅助填写。')
                        task.status='interrupted';task.payload=dump(values);task.updated_at=utcnow()
                # 快照已在本机时，断线不打断独立下载任务。


def install_collector_bridge(app: FastAPI, root: Path):
    bridge = CollectorBridge(root)
    app.state.collector_bridge = bridge

    @app.get("/collector-auto.js")
    def script():
        return FileResponse(root/"src/pintan/static/collector-auto.js", media_type="application/javascript", headers={"Cache-Control":"no-store"})

    @app.get("/api/collector/bridge")
    def state():
        return bridge.bridge_state()

    @app.post("/api/collector/bridge/pair-code")
    def pair_code(request: Request):
        local_write(request)
        with bridge.guard:
            bridge.pair_code = secrets.token_hex(4).upper()
            bridge.pair_until = time.monotonic()+300
            bridge.pair_attempts = 0
            return dict(code=bridge.pair_code, expires_in=300)

    @app.post("/api/collector/bridge/pair")
    def pair(request: Request, payload: dict = Body(...)):
        origin = request.headers.get("origin", "")
        if not extension_origin(origin) or len(dump(payload)) > 1000:
            raise HTTPException(403, "只允许浏览器扩展配对")
        return bridge.pair(payload.get("code", ""), origin, payload.get("name", "采集助手"), payload.get("version"))

    @app.websocket("/api/collector/bridge/ws")
    async def socket(ws: WebSocket):
        await bridge.websocket(ws)

    @app.post("/api/collector/tasks", status_code=202)
    async def submit(request: Request, payload: UrlSubmission):
        local_write(request)
        return await bridge.submit(payload.page_url,payload.mode)

    @app.post('/api/batch-selection/plans')
    def batch_plan(request:Request,payload:dict=Body(...)):
        local_write(request)
        from .batch_selection import recommend_filters
        try:return recommend_filters(root,str(payload.get('keyword','')))
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.post('/api/trend-discovery/plans')
    def trend_plan(request:Request,payload:dict=Body(...)):
        local_write(request)
        from .trend_discovery import suggest_filters
        try:return suggest_filters(str(payload.get('category','')),root)
        except ValueError as exc:raise HTTPException(422,str(exc)) from None

    @app.post('/api/trend-discovery/batches',status_code=202)
    async def trend_start(request:Request,payload:dict=Body(...)):
        local_write(request)
        return await bridge.submit_trend_discovery(payload.get('category'),payload.get('filters') or {})

    @app.get('/api/trend-discovery/batches')
    def trend_batches():
        with session_scope(root) as s:
            ids=list(s.scalars(select(TrendDiscoveryBatch.batch_id).order_by(TrendDiscoveryBatch.created_at.desc()).limit(30)))
        return {'items':[bridge.read_trend_discovery(x) for x in ids]}

    @app.get('/api/trend-discovery/batches/{batch_id}')
    def trend_batch_get(batch_id:str):return bridge.read_trend_discovery(batch_id)

    @app.get('/api/trend-discovery/covers/{batch_id}/{job_id}/{token}')
    def trend_reference(batch_id:str,job_id:str,token:str):
        from .trend_supply import reference_file
        try:path=reference_file(root,batch_id,job_id,token)
        except ValueError as exc:raise HTTPException(404,str(exc)) from None
        return FileResponse(path,media_type='image/jpeg',headers={'Cache-Control':'no-store'})

    @app.post('/api/trend-discovery/batches/{batch_id}/resume')
    async def trend_batch_resume(batch_id:str,request:Request):
        local_write(request)
        with bridge.guard,session_scope(root) as s:
            batch=s.get(TrendDiscoveryBatch,batch_id)
            if not batch:raise HTTPException(404,'爆款采集批次不存在')
            latest=[hid for hid in bridge.connections if s.get(CollectorHelper,hid).version==VERSION]
            if not latest:raise HTTPException(409,f'爆款采集要求采集助手{VERSION}，请重新加载新版扩展')
            batch.helper_id=sorted(latest)[0];progress=json.loads(batch.progress_json)
            for state in progress.get('platforms',{}).values():
                if state.get('status') in {'opening','reading','waiting_user','failed'}:state.update(status='queued',lease='')
            result=json.loads(batch.result_json or '{}');supply=False
            for job in result.get('supply_jobs',[]):
                if job.get('status') in {'opening','reading','waiting_user','failed'}:job.update(status='queued',lease='');supply=True
                elif job.get('status')=='queued':supply=True
            batch.result_json=dump(result);batch.status='supply_search' if supply else 'queued';progress['message']='已继续未完成任务';batch.progress_json=dump(progress);batch.updated_at=utcnow();helper=batch.helper_id
        await bridge.dispatch(helper);return bridge.read_trend_discovery(batch_id)

    @app.post('/api/trend-discovery/batches/{batch_id}/pause')
    async def trend_batch_pause(batch_id:str,request:Request):
        local_write(request)
        with bridge.guard,session_scope(root) as s:
            batch=s.get(TrendDiscoveryBatch,batch_id)
            if not batch:raise HTTPException(404,'爆款采集批次不存在')
            if batch.status in {'completed','partial','failed','cancelled','完成','部分完成'}:
                raise HTTPException(409,'当前任务已结束，不能暂停')
            progress=json.loads(batch.progress_json)
            for state in progress.get('platforms',{}).values():
                if state.get('status') in {'opening','reading'}:state.update(status='queued',lease='')
            result=json.loads(batch.result_json or '{}')
            for job in result.get('supply_jobs',[]):
                if job.get('status') in {'opening','reading'}:job.update(status='queued',lease='')
            progress.update(stage='paused',message='已暂停；页面证据与评分结果均已保留')
            batch.progress_json=dump(progress);batch.result_json=dump(result);batch.status='paused';batch.updated_at=utcnow()
        return bridge.read_trend_discovery(batch_id)

    @app.post('/api/trend-discovery/batches/{batch_id}/cancel')
    async def trend_batch_cancel(batch_id:str,request:Request):
        local_write(request)
        with bridge.guard,session_scope(root) as s:
            batch=s.get(TrendDiscoveryBatch,batch_id)
            if not batch:raise HTTPException(404,'爆款采集批次不存在')
            batch.status='cancelled';progress=json.loads(batch.progress_json);progress.update(stage='cancelled',message='已取消')
            batch.progress_json=dump(progress);batch.updated_at=utcnow()
        return bridge.read_trend_discovery(batch_id)

    @app.post('/api/batch-selection',status_code=202)
    async def batch_start(request:Request,payload:dict=Body(...)):
        local_write(request)
        return await bridge.submit_batch(payload.get('original_keyword'),payload.get('normalized_keyword'),
            payload.get('filters') or {},payload.get('target_count',20),payload.get('max_candidates',60))

    @app.post('/api/batch-selection/image',status_code=202)
    async def image_batch_start(request:Request,payload:dict=Body(...)):
        local_write(request)
        return await bridge.submit_image_batch(payload.get('original_keyword'),payload.get('filters') or {},
            payload.get('image_base64'),payload.get('mime_type'),payload.get('target_count',20),payload.get('max_candidates',60))

    @app.get('/api/batch-selection/images/{batch_id}/{token}')
    def image_batch_reference(batch_id:str,token:str):
        """仅供已下发任务的本机扩展读取用户选择的参考图；不暴露目录浏览。"""
        with session_scope(root) as s:
            batch=s.get(BatchSelection,batch_id)
            if not batch:raise HTTPException(404,'参考图不存在')
            image=json.loads(batch.payload).get('reference_image',{})
        if not image or not secrets.compare_digest(str(token),str(image.get('token',''))):
            raise HTTPException(404,'参考图不存在')
        path=(root/str(image.get('path',''))).resolve();allowed=(root/'data'/'image-search').resolve()
        if allowed not in path.parents or not path.is_file():raise HTTPException(404,'参考图文件不存在')
        return FileResponse(path,media_type=str(image.get('mime_type','application/octet-stream')),headers={'Cache-Control':'no-store'})

    @app.get('/api/batch-selection')
    def batch_list():
        with session_scope(root) as s:
            ids=list(s.scalars(select(BatchSelection.batch_id).order_by(BatchSelection.created_at.desc()).limit(30)))
        return {'items':[bridge.read_batch(x) for x in ids]}

    @app.get('/api/batch-selection/{batch_id}')
    def batch_get(batch_id:str):return bridge.read_batch(batch_id)

    @app.post('/api/batch-selection/{batch_id}/resume')
    async def batch_resume(batch_id:str,request:Request):
        local_write(request);return await bridge.resume_batch(batch_id)

    @app.post('/api/batch-selection/{batch_id}/cancel')
    async def batch_cancel(batch_id:str,request:Request):
        local_write(request);return await bridge.cancel_batch(batch_id)

    @app.post('/api/collector/assets-tasks',status_code=202)
    async def assets_tasks(request:Request,payload:dict=Body(...)):
        local_write(request);return await bridge.submit_assets_for_products(payload.get('product_ids'))

    @app.get("/api/collector/tasks")
    def tasks():
        with session_scope(root) as s:
            ids = list(s.scalars(select(CollectorTask.task_id).order_by(CollectorTask.created_at.desc(), CollectorTask.task_id.desc()).limit(100)))
        return dict(items=[bridge.read(t) for t in ids])

    @app.get("/api/collector/tasks/{task_id}")
    def task(task_id: str):
        return bridge.read(task_id)

    @app.post("/api/collector/tasks/{task_id}/resume")
    async def resume(task_id: str, request: Request):
        local_write(request)
        return await bridge.resume(task_id)

    @app.post("/api/collector/tasks/{task_id}/cancel")
    async def cancel(task_id: str, request: Request):
        local_write(request)
        return await bridge.cancel(task_id)

    @app.get("/api/collector/bridge/extension.zip")
    def extension_zip():
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in ["manifest.json", "background.js", "capture.js", "search_capture.js", "image_search.js", "trend_search_capture.js", "listing_fill.js", "popup.html", "popup.js", "README.md"]:
                archive.write(root/"extension"/name, "pintan-collector/"+name)
        return Response(buffer.getvalue(), media_type="application/zip", headers={"Content-Disposition":"attachment; filename=pintan-collector.zip"})

    async def shutdown():
        bridge.closing=True
        for task_id, worker in list(bridge.workers.items()):
            bridge.change(task_id, "interrupted", error_type="server_restarted", message=ERRORS["server_restarted"])
            worker.cancel()
        for batch_id,worker in list(bridge.batch_workers.items()):
            bridge.change_batch(batch_id,'interrupted',phase='interrupted',message='服务正在重启；评分进度已保留。')
            worker.cancel()
    original_lifespan=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            try:
                yield
            finally:
                await shutdown()
    app.router.lifespan_context=lifespan
