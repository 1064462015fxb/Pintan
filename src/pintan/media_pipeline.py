"""商品原图采集、授权门禁、图片合成与15秒轮播视频。

这个模块不让生成模型重绘商品：Qwen 只生成无商品背景，真实商品
主体由 rembg 抠图后通过 Pillow 合成，文字也由 Pillow 渲染。
"""
from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import uuid
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import func, select

from .database import (AssetAuthorization, ContentMaster, GeneratedMedia, PlatformContent,
    PlatformContentVersion, Product, ProductAsset, session_scope, utcnow)
from .asset_records import metadata, update_metadata

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MIN_IMAGE_SIDE = 200  # 历史1688缩略图可低至220px；仍拒绝图标与跟踪像素。
IMAGE_MODEL_VERSION = "media-image-v1.0"
VIDEO_VERSION = "slideshow-v1.0"
PLATFORM_SIZES = {"淘宝": (1200,1200), "京东": (1200,1200), "Amazon": (1200,1200),
                  "抖音": (1080,1920), "小红书": (1080,1440)}
QWEN_SIZES = {"淘宝":"2048*2048", "京东":"2048*2048", "Amazon":"2048*2048",
              "抖音":"1536*2688", "小红书":"1728*2368"}
_REMBG_SESSION = None


def _sha(data: bytes) -> str: return hashlib.sha256(data).hexdigest()


def _safe_public_url(url: str, *, page: bool=False) -> str:
    """SSRF 防护：限制协议、端口、凭据与私有/本机解析地址。"""
    parsed=urlsplit(str(url).strip())
    if parsed.scheme not in {"http","https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("只允许无凭据的HTTP/HTTPS地址")
    if parsed.port not in {None,80,443}: raise ValueError("不允许非标准端口")
    host=parsed.hostname.lower().rstrip(".")
    if page and host not in {"detail.1688.com","detail.m.1688.com"}:
        raise ValueError("商品页必须是1688详情页")
    try: addresses=socket.getaddrinfo(host,parsed.port or (443 if parsed.scheme=="https" else 80))
    except socket.gaierror as exc: raise ValueError("地址无法解析") from exc
    for item in addresses:
        address=ipaddress.ip_address(item[4][0])
        if not address.is_global: raise ValueError("不允许内网或本机地址")
    return parsed.geturl()


def _extract_images(html: str, page_url: str) -> list[str]:
    """只从当前页面HTML的图片字段取值，不执行页内脚本。"""
    candidates=[]
    patterns=[r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)',
              r'["\'](?:image|imageUrl|mainImage|fullPathImageURI)["\']\s*:\s*["\']([^"\']+)',
              r'<img[^>]+(?:src|data-src)=["\']([^"\']+)']
    for pattern in patterns:
        candidates.extend(re.findall(pattern,html,re.I))
    result=[]
    for raw in candidates:
        url=urljoin(page_url,raw.replace("\\/","/").replace("&amp;","&"))
        if not re.search(r'\.(?:jpe?g|png|webp)(?:\?|$)',url,re.I): continue
        if url not in result: result.append(url)
    return result[:12]


def _ocr(data: bytes, *, strict:bool=False) -> str:
    """OCR是可选处理；任何异常都只产生空的待复核结果。"""
    if os.getenv("PINTAN_OCR_ENABLED","true").lower() not in {"1","true","yes"}:return ""
    try:
        from .ocr_worker import worker
        return worker.recognize(data, timeout=max(5, min(180, float(os.getenv('PINTAN_OCR_TIMEOUT', '90')))))
    except Exception:
        from .task_control import checkpoint
        checkpoint()
        if strict:raise
        return ""


def _cutout(data: bytes) -> bytes:
    from .task_control import checkpoint
    checkpoint()
    global _REMBG_SESSION
    if os.getenv("PINTAN_CUTOUT_ENGINE","rembg").lower()=="grabcut":
        # 仅用于CPU极慢环境的显式测试降级；结果血缘会记录该引擎。
        import cv2, numpy as np
        image=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
        if image is None:raise ValueError("无法解码抠图原图")
        height,width=image.shape[:2];mask=np.zeros((height,width),np.uint8)
        bg=np.zeros((1,65),np.float64);fg=np.zeros((1,65),np.float64)
        margin=max(2,min(width,height)//25)
        cv2.grabCut(image,mask,(margin,margin,width-2*margin,height-2*margin),bg,fg,5,cv2.GC_INIT_WITH_RECT)
        alpha=np.where((mask==2)|(mask==0),0,255).astype("uint8")
        # 只保留最大连通主体，去掉原图中与商品分离的参数牌、角标和碎片。
        count,labels,stats,_=cv2.connectedComponentsWithStats(alpha,8)
        if count>1:
            largest=1+int(np.argmax(stats[1:,cv2.CC_STAT_AREA]));alpha=np.where(labels==largest,255,0).astype("uint8")
        rgba=cv2.cvtColor(image,cv2.COLOR_BGR2BGRA);rgba[:,:,3]=alpha
        ok,encoded=cv2.imencode(".png",rgba)
        if not ok or not alpha.any():raise ValueError("抠图未识别到商品主体")
        return _trim_subject(encoded.tobytes())
    from rembg import new_session, remove
    from PIL import Image
    if _REMBG_SESSION is None:
        # u2netp 体积远小于默认大模型；复用会话，避免每张图重复初始化。
        import onnxruntime as ort
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        _REMBG_SESSION=new_session("u2netp",sess_opts=options,providers=["CPUExecutionProvider"])
    result=remove(data,session=_REMBG_SESSION)
    image=Image.open(io.BytesIO(result)).convert("RGBA")
    if image.getbbox() is None: raise ValueError("抠图未识别到商品主体")
    output=io.BytesIO(); image.save(output,format="PNG"); return _trim_subject(output.getvalue())


def _trim_subject(data:bytes)->bytes:
    """按透明通道包围盒裁切，避免主体因原图四周留白而在成图中过小。"""
    from PIL import Image
    image=Image.open(io.BytesIO(data)).convert("RGBA");bbox=image.getchannel("A").getbbox()
    if not bbox:raise ValueError("抠图未识别到商品主体")
    image=image.crop(bbox);output=io.BytesIO();image.save(output,"PNG");return output.getvalue()


def _background_quality(data:bytes,slot:str)->tuple[bool,str]:
    """无需相信模型声明，使用边缘密度拒绝主图/卖点图中央的大型复杂物体。"""
    import cv2,numpy as np
    image=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
    if image is None:return False,"背景无法解码"
    height,width=image.shape[:2];center=image[int(height*.18):int(height*.82),int(width*.15):int(width*.85)]
    gray=cv2.cvtColor(center,cv2.COLOR_BGR2GRAY);edges=cv2.Canny(gray,80,180)
    density=float((edges>0).mean())
    limit=.065 if slot=="场景图" else .035
    return (density<=limit,f"背景复杂度{density:.3f}，上限{limit:.3f}")


def collect_assets(root: Path, product_id: str) -> dict:
    with session_scope(root) as s:
        product=s.get(Product,product_id)
        if not product or not product.source_product_url: raise ValueError("商品没有可采集的1688详情链接")
        page_url=_safe_public_url(product.source_product_url,page=True)
        # 浏览器采集 Agent 已获得的图片 URL 优先使用。它们来自登录态页面，
        # 后端只下载公开 CDN 图片，不接收或复用 Cookie。
        captured_urls=[x.source_url for x in s.scalars(select(ProductAsset).where(
            ProductAsset.product_id==product_id,ProductAsset.source_url.is_not(None),
            ProductAsset.local_path.is_(None))) if x.source_url]
    headers={"User-Agent":"Mozilla/5.0 PintanAssetCollector/1.0"}
    with httpx.Client(timeout=25,follow_redirects=True,headers=headers) as client:
        urls=list(dict.fromkeys(captured_urls))
        try:
            response=client.get(page_url); response.raise_for_status()
            if "text/html" in response.headers.get("content-type","").lower():
                urls=list(dict.fromkeys(urls+_extract_images(response.text,str(response.url))))
        except Exception:
            # 登录态页面不可由后端直接读取时，仍可处理浏览器已经发现的公开图片。
            pass
        if not urls: raise ValueError("未读取到商品图，可能需要登录，请使用人工上传")
        folder=root/"data/media"/product_id/"source"; folder.mkdir(parents=True,exist_ok=True)
        saved=[]; seen=set()
        for index,url in enumerate(urls):
            try:
                safe=_safe_public_url(url); image_response=client.get(safe)
                image_response.raise_for_status(); data=image_response.content
                content_type=image_response.headers.get("content-type","").split(";",1)[0].lower()
                if content_type not in {"image/jpeg","image/png","image/webp"} or not (0<len(data)<=MAX_IMAGE_BYTES): continue
                from PIL import Image
                with Image.open(io.BytesIO(data)) as img:
                    img.verify(); width,height=img.size
                if width<MIN_IMAGE_SIDE or height<MIN_IMAGE_SIDE: continue
                digest=_sha(data)
                if digest in seen: continue
                seen.add(digest); ext={"image/jpeg":"jpg","image/png":"png","image/webp":"webp"}[content_type]
                path=folder/f"source-{index+1:02d}.{ext}"; path.write_bytes(data)
                cutout_path=folder/f"cutout-{index+1:02d}.png"; cutout_status="ready"
                try: cutout_path.write_bytes(_cutout(data))
                except Exception: cutout_path=None; cutout_status="failed"
                aid="AS-"+uuid.uuid4().hex[:16]
                with session_scope(root) as s:
                    duplicate=s.scalar(select(ProductAsset).where(ProductAsset.product_id==product_id,ProductAsset.sha256==digest))
                    if duplicate:
                        saved.append({"asset_id":duplicate.asset_id,"width":duplicate.width,"height":duplicate.height,"cutout_status":"ready" if duplicate.cutout_path else "failed","deduplicated":True})
                        continue
                    s.add(ProductAsset(asset_id=aid,product_id=product_id,asset_type="原图" if index==0 else "详情图",
                        status="待授权确认" if cutout_path else "抠图失败",source_url=safe,
                        local_path=str(path.relative_to(root)),cutout_path=str(cutout_path.relative_to(root)) if cutout_path else None,
                        sha256=digest,mime_type=content_type,byte_size=len(data),width=width,height=height,collected_at=utcnow(),
                        ocr_text=_ocr(data),authorization_status="未确认"))
                saved.append({"asset_id":aid,"width":width,"height":height,"cutout_status":cutout_status})
            except Exception: continue
            if len(saved)>=6: break
    if not saved: raise ValueError("商品页图片均未通过安全与格式校验")
    return {"product_id":product_id,"assets":saved,"count":len(saved)}


def upload_asset(root:Path,product_id:str,data:bytes,declared_mime:str="",asset_type:str="人工上传原图")->dict:
    """人工补图：保留目标素材类型与人工来源，不把上传图片伪装成1688采集。"""
    allowed_types={"人工上传原图","信息图","功能图","参数图","场景图","主图","SKU图","详情图"}
    if asset_type not in allowed_types:raise ValueError("不支持的素材类型")
    if not data or len(data)>MAX_IMAGE_BYTES:raise ValueError("上传图片为空或超过10MB")
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify();width,height=image.size;fmt=image.format
    except Exception as exc:raise ValueError("上传内容不是有效图片") from exc
    mime={"JPEG":"image/jpeg","PNG":"image/png","WEBP":"image/webp"}.get(fmt or "")
    if not mime or (declared_mime and declared_mime not in {mime,"application/octet-stream"}):raise ValueError("仅支持JPEG、PNG和WEBP")
    if width<MIN_IMAGE_SIDE or height<MIN_IMAGE_SIDE:raise ValueError(f"图片像素不得低于{MIN_IMAGE_SIDE}×{MIN_IMAGE_SIDE}")
    digest=_sha(data);ext={"image/jpeg":"jpg","image/png":"png","image/webp":"webp"}[mime]
    with session_scope(root) as s:
        if not s.get(Product,product_id):raise ValueError("商品不存在")
        duplicate=s.scalar(select(ProductAsset).where(ProductAsset.product_id==product_id,ProductAsset.sha256==digest))
        if duplicate:
            # 同一个文件无需落盘两次，但用户可能是在为另一项“缺失素材需求”
            # 指定它。保留原来的采集类别，并把人工指定的用途作为可追溯关联。
            existing=metadata(s,duplicate.asset_id)
            targets=set(existing.get("manual_target_asset_types",[]))
            targets.add(asset_type)
            update_metadata(s,duplicate.asset_id,manual_target_asset_types=sorted(targets))
            return {"product_id":product_id,"asset_id":duplicate.asset_id,
                    "asset_type":asset_type,"width":duplicate.width,"height":duplicate.height,
                    "deduplicated":True,"target_associated":True}
    folder=root/"data/media"/product_id/"source";folder.mkdir(parents=True,exist_ok=True)
    path=folder/f"upload-{digest[:12]}.{ext}";path.write_bytes(data)
    # 参数图、功能图或纯场景图未必出现完整商品主体；抠图失败不能阻止人工补图。
    cutout_path=None;cutout_status="不适用或未识别主体"
    try:
        cutout=_cutout(data);cutout_file=folder/f"cutout-{digest[:12]}.png";cutout_file.write_bytes(cutout)
        cutout_path=str(cutout_file.relative_to(root));cutout_status="已生成"
    except Exception:pass
    aid="AS-"+uuid.uuid4().hex[:16]
    with session_scope(root) as s:
        s.add(ProductAsset(asset_id=aid,product_id=product_id,asset_type=asset_type,
            status="待授权确认",local_path=str(path.relative_to(root)),cutout_path=cutout_path,
            sha256=digest,mime_type=mime,byte_size=len(data),width=width,height=height,collected_at=utcnow(),
            ocr_text=_ocr(data),authorization_status="未确认"))
        s.flush();update_metadata(s,aid,source_kind="人工上传",target_asset_type=asset_type,
                                  manual_target_asset_types=[asset_type],
                                  cutout_status=cutout_status,verification_status="待复核")
    return {"product_id":product_id,"asset_id":aid,"asset_type":asset_type,"width":width,"height":height,"cutout_status":cutout_status,"deduplicated":False}


def authorize_asset(root:Path,asset_id:str,confirmed:bool,statement:str)->dict:
    if not confirmed: raise ValueError("必须明确确认拥有素材使用权")
    expected="我已确认拥有该图片的使用权"
    if statement.strip() not in {expected,'用户已授权当前商品原图用于本次生成测试，不代表平台或权利人证明'}: raise ValueError("授权声明不匹配")
    expected=statement.strip()
    with session_scope(root) as s:
        asset=s.get(ProductAsset,asset_id)
        if not asset or not asset.sha256 or not asset.local_path: raise ValueError("原图不存在或不完整")
        old=s.scalar(select(AssetAuthorization).where(AssetAuthorization.asset_id==asset_id,
            AssetAuthorization.asset_sha256==asset.sha256))
        if not old:s.add(AssetAuthorization(authorization_id="AU-"+uuid.uuid4().hex[:16],asset_id=asset_id,
            asset_sha256=asset.sha256,confirmed=True,statement=expected,confirmed_at=utcnow()))
        asset.authorization_status="已确认"
        if '本次生成测试' in expected:update_metadata(s,asset.asset_id,authorization_scope='test_only')
    return {"asset_id":asset_id,"authorized":True}


def authorize_product_assets(root:Path,product_id:str,asset_ids:list[str],confirmed:bool,statement:str)->dict:
    """一次确认一个商品的当前有效图片；授权仍逐文件哈希绑定。"""
    if not confirmed:raise ValueError("必须明确确认拥有素材使用权")
    expected="我已确认拥有该商品当前所选图片的使用权"
    if statement.strip()!=expected:raise ValueError("商品图片授权声明不匹配")
    clean=list(dict.fromkeys(str(x).strip() for x in (asset_ids or []) if str(x).strip()))
    if not clean:raise ValueError("没有选择需要授权的图片")
    with session_scope(root) as s:
        assets=[]
        for asset_id in clean:
            asset=s.get(ProductAsset,asset_id)
            if not asset or asset.product_id!=product_id:raise ValueError("图片不属于当前商品")
            if not asset.sha256 or not asset.local_path or asset.status=='复核排除' or asset.status.startswith('已合并:'):
                raise ValueError("所选图片不存在、未下载或已被排除")
            assets.append(asset)
        for asset in assets:
            old=s.scalar(select(AssetAuthorization).where(AssetAuthorization.asset_id==asset.asset_id,
                AssetAuthorization.asset_sha256==asset.sha256))
            if old:
                old.confirmed=True;old.statement=expected;old.confirmed_at=utcnow()
            else:s.add(AssetAuthorization(authorization_id="AU-"+uuid.uuid4().hex[:16],asset_id=asset.asset_id,
                asset_sha256=asset.sha256,confirmed=True,statement=expected,confirmed_at=utcnow()))
            asset.authorization_status="已确认"
    return {"product_id":product_id,"authorized":len(assets),"asset_ids":[x.asset_id for x in assets]}


def _platform_record(root:Path,content_id:str):
    with session_scope(root) as s:
        content=s.get(PlatformContent,content_id)
        if not content: raise ValueError("平台内容不存在")
        master=s.get(ContentMaster,content.master_id)
        version=s.scalar(select(PlatformContentVersion).where(PlatformContentVersion.content_id==content_id,
            PlatformContentVersion.version==content.current_version))
        return content,master,json.loads(version.visible_json),version.version


def _authorized_source(root:Path,product_id:str):
    with session_scope(root) as s:
        assets=list(s.scalars(select(ProductAsset).where(ProductAsset.product_id==product_id,
            ProductAsset.authorization_status=="已确认",ProductAsset.local_path.is_not(None))))
        # SKU图通常主体更大、更单一；其后依次选择主图和详情/功能图。
        assets.sort(key=lambda x:({'SKU图':0,'主图':1}.get(x.asset_type,2),x.asset_id))
        for asset in assets:
            meta=metadata(s,asset.asset_id)
            effective_type=meta.get('review_asset_type',asset.asset_type)
            if (asset.status=='复核排除' or asset.status.startswith('已合并:') or meta.get('review_included') is False or
                    (effective_type=='待分类商品图' and max(asset.width or 0,asset.height or 0)<=256)):
                continue
            auth=s.scalar(select(AssetAuthorization).where(AssetAuthorization.asset_id==asset.asset_id,
                AssetAuthorization.asset_sha256==asset.sha256,AssetAuthorization.confirmed==True))
            path=(root/(asset.cutout_path or asset.local_path)).resolve() if asset.local_path else None
            # 扩展下载不做抠图；生成时才处理已授权原图，避免下载成功却永远不能生成。
            if auth and not asset.cutout_path and path and path.is_relative_to(root.resolve()) and path.is_file():
                target=path.parent/f'{asset.sha256}-cutout.png'
                target.write_bytes(_cutout(path.read_bytes()))
                asset.cutout_path=str(target.relative_to(root.resolve()));path=target
                update_metadata(s,asset.asset_id,cutout_engine=os.getenv('PINTAN_CUTOUT_ENGINE','rembg'))
            if auth and asset.cutout_path and path and path.is_relative_to(root.resolve()) and path.is_file(): return asset,path
    raise ValueError("没有与当前原图哈希绑定的使用权确认")


def _generate_background(prompt:str,size:str)->bytes:
    key=os.getenv("QWEN_API_KEY","").strip()
    if not key: raise RuntimeError("未设置QWEN_API_KEY")
    endpoint=os.getenv("QWEN_IMAGE_BASE_URL","https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
    payload={"model":os.getenv("QWEN_IMAGE_MODEL","qwen-image-2.0"),
        "input":{"messages":[{"role":"user","content":[{"text":prompt}]}]},
        "parameters":{"size":size,"n":1,"prompt_extend":False,
            "negative_prompt":"商品，包装，品牌，logo，文字，水印，人物，多余物体"}}
    with httpx.Client(timeout=120,follow_redirects=True) as client:
        response=client.post(endpoint,headers={"Authorization":f"Bearer {key}","Content-Type":"application/json"},json=payload)
        response.raise_for_status(); body=response.json()
        blocks=body.get("output",{}).get("choices",[{}])[0].get("message",{}).get("content",[])
        image_url=next((x.get("image") for x in blocks if isinstance(x,dict) and x.get("image")),None)
        if not image_url: raise ValueError("图片模型未返回图片")
        result=client.get(_safe_public_url(image_url)); result.raise_for_status()
        if len(result.content)>MAX_IMAGE_BYTES: raise ValueError("生成图超过大小限制")
        return result.content


def _font(size:int):
    from PIL import ImageFont
    for path in ("C:/Windows/Fonts/msyhbd.ttc","C:/Windows/Fonts/msyh.ttc","C:/Windows/Fonts/arial.ttf"):
        if Path(path).exists(): return ImageFont.truetype(path,size)
    return ImageFont.load_default()


def _compose(background:bytes,subject_path:Path,target:tuple[int,int],headline:str,points:list[str],slot:str='主图')->bytes:
    from PIL import Image,ImageDraw,ImageFilter
    canvas=Image.open(io.BytesIO(background)).convert("RGB").resize(target,Image.Resampling.LANCZOS)
    # 抠图是硬门槛：失败时不能把带原背景的矩形图片伪装成分层合成结果。
    subject=Image.open(subject_path).convert("RGBA")
    # 去掉抠图边缘的低透明残留，再按有效主体裁边；不修改商品颜色/结构。
    alpha=subject.getchannel('A');alpha=alpha.point(lambda v:0 if v<80 else v);subject.putalpha(alpha)
    bbox=alpha.getbbox()
    if not bbox:raise ValueError('缺少有效商品主体')
    subject=subject.crop(bbox)
    max_w,max_h=int(target[0]*.68),int(target[1]*.58)
    scale=min(max_w/subject.width,max_h/subject.height)
    subject=subject.resize((max(1,int(subject.width*scale)),max(1,int(subject.height*scale))),Image.Resampling.LANCZOS)
    x=(target[0]-subject.width)//2
    y=int(target[1]*.79)-subject.height if slot=='场景图' else int(target[1]*.22)
    shadow=Image.new("RGBA",target,(0,0,0,0)); shadow.alpha_composite(subject,(x+12,y+16)); shadow=shadow.filter(ImageFilter.GaussianBlur(18))
    canvas=Image.alpha_composite(canvas.convert("RGBA"),shadow); canvas.alpha_composite(subject,(x,y))
    draw=ImageDraw.Draw(canvas); margin=int(target[0]*.06)
    draw.rounded_rectangle((margin,margin,target[0]-margin,margin+int(target[1]*.10)),radius=24,fill=(255,255,255,225))
    draw.text((margin+24,margin+18),headline[:22],font=_font(max(30,int(target[0]*.038))),fill=(18,57,46,255))
    for i,point in enumerate(points[:3]): draw.text((margin+24,int(target[1]*.84)+i*44),"· "+point[:26],font=_font(25),fill=(255,255,255,255),stroke_width=2,stroke_fill=(25,45,38,180))
    output=io.BytesIO(); canvas.convert("RGB").save(output,"PNG"); return output.getvalue()


def generate_images(root:Path,content_id:str,slots:list[str]|None=None,*,reuse_backgrounds:bool=False)->dict:
    root=root.resolve()
    content,master,fields,content_version=_platform_record(root,content_id)
    from .content_flow import scan
    from .database import product_bundle
    if scan(fields,product_bundle(root,master.product_id),content.platform):raise ValueError('文案包含未解决风险，请先修改文案再生成图片')
    asset,source_path=_authorized_source(root,master.product_id)
    slots=slots or ["主图","场景图","卖点图"]
    if any(x not in {"主图","场景图","卖点图"} for x in slots): raise ValueError("未知图片槽位")
    title=str(fields.get("短标题") or fields.get("商品标题") or fields.get("Product Title") or master.product_id)
    raw_points=fields.get("核心卖点") or fields.get("卖点") or fields.get("Bullet Points") or []
    points=raw_points if isinstance(raw_points,list) else [str(raw_points)]
    folder=root/"data/media"/master.product_id/"generated"/content_id; folder.mkdir(parents=True,exist_ok=True)
    output=[]
    for slot in slots:
        version=1
        with session_scope(root) as s:
            highest=s.scalar(select(func.max(GeneratedMedia.version)).where(GeneratedMedia.product_id==master.product_id,
                GeneratedMedia.content_id==content_id,GeneratedMedia.media_type=="image",GeneratedMedia.slot==slot))
            version=(highest or 0)+1
        mid="MD-"+uuid.uuid4().hex[:16];attempts=[]
        try:
            prompt={"主图":"完全空的浅灰白无缝摄影棚背景，仅有地面与柔和阴影，不得出现任何物体",
                    "场景图":"空置的极简桌面与浅色墙面，桌面中央大片留白，不得出现盒子瓶子电器",
                    "卖点图":"纯抽象浅色渐变背景，底部深色渐变，除色块外不得出现任何物体"}[slot]
            background=None
            if reuse_backgrounds:
                existing=sorted(folder.glob(f'{slot}-background-v*.png'),key=lambda p:p.stat().st_mtime,reverse=True)
                if existing:
                    candidate=existing[0].read_bytes()
                    accepted,reason=_background_quality(candidate,slot)
                    if accepted:background=candidate;attempts.append({'reused_background':existing[0].name,'accepted':True,'check':reason})
            for attempt in range(2):
                if background is not None:break
                candidate=_generate_background(prompt+"。这是空背景素材：零商品、零包装、零品牌、零logo、零文字、零水印、零人物。",QWEN_SIZES[content.platform])
                accepted,reason=_background_quality(candidate,slot);attempts.append({"attempt":attempt+1,"accepted":accepted,"check":reason})
                if accepted:background=candidate;break
            if background is None:raise ValueError("两次背景均未通过复杂度检查")
            background_path=folder/f"{slot}-background-v{version}.png";background_path.write_bytes(background)
            data=_compose(background,source_path,PLATFORM_SIZES[content.platform],title,points if slot=="卖点图" else [],slot)
            path=folder/f"{slot}-v{version}.png";path.write_bytes(data);status="success";error=None
        except Exception as exc:
            path=None;data=b"";status="failed";error=type(exc).__name__
        metadata={"platform":content.platform,"size":PLATFORM_SIZES[content.platform],"content_version":content_version,
            "model":os.getenv("QWEN_IMAGE_MODEL","qwen-image-2.0"),"pipeline_version":IMAGE_MODEL_VERSION,
            "cutout_engine":os.getenv("PINTAN_CUTOUT_ENGINE","rembg"),"background_checks":attempts,"error_type":error,
            "background_path":str(background_path.relative_to(root)) if status=="success" else None}
        with session_scope(root) as s:s.add(GeneratedMedia(media_id=mid,product_id=master.product_id,content_id=content_id,
            media_type="image",slot=slot,version=version,status=status,local_path=str(path.relative_to(root)) if path else None,
            sha256=_sha(data) if data else None,source_asset_id=asset.asset_id,source_sha256=asset.sha256,
            metadata_json=json.dumps(metadata,ensure_ascii=False),created_at=utcnow()))
        output.append({"media_id":mid,"slot":slot,"version":version,"status":status,"error_type":error})
    return {"content_id":content_id,"items":output}


def _video_lines(fields:dict)->list[str]:
    """从已审核的平台字段提取三段字幕，不添加任何新规格或功效。"""
    preferred=("视频钩子","口播","字幕","商品卡卖点","核心卖点","卖点","Bullet Points")
    values=[]
    for key in preferred:
        value=fields.get(key)
        if isinstance(value,list): values.extend(str(x) for x in value)
        elif value: values.append(str(value))
    clean=[re.sub(r"\s+"," ",x).strip()[:32] for x in values if str(x).strip()]
    return (clean+["商品信息以页面参数为准"]*3)[:3]


def _tts_audio(text:str)->tuple[bytes|None,str]:
    """尝试OpenAI兼容TTS；配置缺失或服务不兼容时返回静音降级状态。"""
    endpoint=os.getenv("QWEN_TTS_BASE_URL","").strip()
    key=os.getenv("QWEN_API_KEY","").strip()
    if not endpoint or not key:return None,"tts_not_configured_silent_fallback"
    try:
        with httpx.Client(timeout=120) as client:
            response=client.post(endpoint,headers={"Authorization":f"Bearer {key}"},json={
                "model":os.getenv("QWEN_TTS_MODEL","qwen-audio-3.0-tts-plus"),
                "input":text,"voice":"Cherry","response_format":"mp3","speed":1.0})
            response.raise_for_status()
            if not response.content:return None,"tts_empty_silent_fallback"
            return response.content,"success"
    except Exception:return None,"tts_failed_silent_fallback"


def _caption_slide(source:Path,target:Path,text:str)->None:
    from PIL import Image,ImageDraw
    image=Image.open(source).convert("RGB")
    draw=ImageDraw.Draw(image,"RGBA"); h=image.height; margin=max(24,image.width//20)
    draw.rounded_rectangle((margin,h-margin-110,image.width-margin,h-margin),radius=18,fill=(0,0,0,165))
    font=_font(max(26,image.width//34)); box=draw.textbbox((0,0),text,font=font)
    draw.text(((image.width-(box[2]-box[0]))//2,h-margin-82),text,font=font,fill="white")
    image.save(target,"PNG")


def generate_video(root:Path,content_id:str)->dict:
    root=root.resolve()
    content,master,fields,content_version=_platform_record(root,content_id)
    with session_scope(root) as s:
        images=[]
        for slot in ("主图","场景图","卖点图"):
            item=s.scalar(select(GeneratedMedia).where(GeneratedMedia.product_id==master.product_id,
                GeneratedMedia.content_id==content_id,GeneratedMedia.media_type=="image",GeneratedMedia.slot==slot,
                GeneratedMedia.status=="success").order_by(GeneratedMedia.version.desc()))
            if not item or not item.local_path: raise ValueError("请先完整生成三张图片")
            images.append((root/item.local_path).resolve())
    ffmpeg=os.getenv("FFMPEG_PATH","").strip()
    if not ffmpeg:
        try:
            import imageio_ffmpeg
            ffmpeg=imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            ffmpeg="ffmpeg"
    folder=root/"data/media"/master.product_id/"generated"/content_id; folder.mkdir(parents=True,exist_ok=True)
    lines=_video_lines(fields); captioned=[]
    for index,(image,line) in enumerate(zip(images,lines),1):
        slide=folder/f"video-slide-{index}.png";_caption_slide(image,slide,line);captioned.append(slide)
    concat=folder/"slides.txt"; concat.write_text("".join(f"file '{p.as_posix()}'\nduration 5\n" for p in captioned)+f"file '{captioned[-1].as_posix()}'\n",encoding="utf-8")
    version=1
    with session_scope(root) as s:
        highest=s.scalar(select(func.max(GeneratedMedia.version)).where(GeneratedMedia.product_id==master.product_id,
            GeneratedMedia.content_id==content_id,GeneratedMedia.media_type=="video")); version=(highest or 0)+1
    path=folder/f"video-v{version}.mp4"
    audio,audio_status=_tts_audio("。".join(lines)); audio_path=folder/f"voice-v{version}.mp3"
    command=[ffmpeg,"-y","-f","concat","-safe","0","-i",str(concat)]
    if audio:
        audio_path.write_bytes(audio);command.extend(["-i",str(audio_path)])
    command.extend(["-t","15","-vf","fps=30,format=yuv420p","-c:v","libx264"])
    if audio:command.extend(["-c:a","aac","-af","apad=pad_dur=15"])
    command.extend(["-movflags","+faststart",str(path)])
    try: subprocess.run(command,check=True,capture_output=True,timeout=120)
    except (OSError,subprocess.SubprocessError) as exc: raise RuntimeError("FFmpeg合成失败") from exc
    data=path.read_bytes(); mid="MD-"+uuid.uuid4().hex[:16]
    metadata={"duration_seconds":15,"content_version":content_version,"pipeline_version":VIDEO_VERSION,
              "audio_status":audio_status,"voice":"Cherry" if audio else None,"subtitles":lines,"codec":"h264"}
    with session_scope(root) as s:s.add(GeneratedMedia(media_id=mid,product_id=master.product_id,content_id=content_id,
        media_type="video",slot="15秒轮播",version=version,status="success",local_path=str(path.relative_to(root)),
        sha256=_sha(data),metadata_json=json.dumps(metadata,ensure_ascii=False),created_at=utcnow()))
    return {"media_id":mid,"content_id":content_id,"duration_seconds":15,"audio_status":metadata["audio_status"]}


def media_product(root:Path,product_id:str)->dict:
    with session_scope(root) as s:
        assets=list(s.scalars(select(ProductAsset).where(ProductAsset.product_id==product_id).order_by(ProductAsset.asset_id)))
        media=list(s.scalars(select(GeneratedMedia).where(GeneratedMedia.product_id==product_id).order_by(GeneratedMedia.created_at.desc())))
        return {"product_id":product_id,"assets":[{"asset_id":x.asset_id,"asset_type":x.asset_type,"status":x.status,
            "ocr_text":x.ocr_text or "","authorization_status":x.authorization_status or "未确认",
            "mime_type":x.mime_type,"byte_size":x.byte_size,"width":x.width,"height":x.height,"collected_at":x.collected_at,
            "preview_url":f"/api/assets/{x.asset_id}/file" if x.local_path else None} for x in assets],
            "media":[{"media_id":x.media_id,"content_id":x.content_id,"media_type":x.media_type,"slot":x.slot,
                "version":x.version,"status":x.status,"confirmed":x.confirmed,"created_at":x.created_at,
                "url":f"/api/media/files/{x.media_id}" if x.local_path else None,"metadata":json.loads(x.metadata_json)} for x in media]}


def confirm_media(root:Path,media_id:str)->dict:
    with session_scope(root) as s:
        item=s.get(GeneratedMedia,media_id)
        if not item: raise ValueError("媒体不存在")
        if item.status!='success':raise ValueError('未通过审核的图片不能确认')
        item.confirmed=True
    return {"media_id":media_id,"confirmed":True}


def resolve_asset_file(root:Path,asset_id:str)->Path:
    with session_scope(root) as s:item=s.get(ProductAsset,asset_id)
    if not item or not item.local_path: raise ValueError("素材不存在")
    path=(root/item.local_path).resolve()
    allowed=[(root/'data/media').resolve(),(root/'data/assets').resolve()]
    if not any(path.is_relative_to(folder) for folder in allowed) or not path.is_file():raise ValueError('素材路径无效')
    return path


def resolve_media_file(root:Path,media_id:str)->Path:
    with session_scope(root) as s:item=s.get(GeneratedMedia,media_id)
    if not item or not item.local_path: raise ValueError("媒体不存在")
    return _resolve_file(root,item.local_path)


def _resolve_file(root:Path,value:str)->Path:
    path=(root/value).resolve(); allowed=(root/"data/media").resolve()
    if not path.is_relative_to(allowed) or not path.is_file(): raise ValueError("媒体路径无效")
    return path
