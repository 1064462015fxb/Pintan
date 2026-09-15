"""采集素材原位下载。原图与后续OCR/抠图分开，下载失败不丢失来源。"""
import hashlib
import io
import re
from pathlib import Path
import httpx
from PIL import Image
from sqlalchemy import select
from .database import ProductAsset, session_scope, utcnow
from .media_pipeline import _safe_public_url, MAX_IMAGE_BYTES
from .asset_records import resolve, update_metadata, metadata

def variants(url):
    """只升级已知缩略后缀，保留原始候选供失败回退。"""
    original=re.sub(r'(\.(?:jpg|png|jpeg))_(?:sum\.jpg|b\.jpg|\.webp|\d+x\d+[^?]*)$',r'\1',url)
    return list(dict.fromkeys([original,url]))

def quality(width,height,kind):
    if (kind=='详情图' and width>=750) or min(width,height)>=800:return '高清可用'
    return '普通可用' if min(width,height)>=600 else '缩略图'

def is_review_image(meta):
    """评论区缩略图是唯一允许进入素材池的低分辨率图片。"""
    regions={str(x.get('region','')) for x in meta.get('region_evidence',[]) if isinstance(x,dict)}
    if meta.get('region'):regions.add(str(meta['region']))
    return '评论图' in regions

def download_asset(root:Path,asset_id:str):
    with session_scope(root) as s:
        asset=resolve(s,s.get(ProductAsset,asset_id))
        if not asset or not asset.source_url:raise ValueError('素材没有来源地址')
        asset_id=asset.asset_id
        url=asset.source_url;pid=asset.product_id
        previous_status=asset.status
        if not asset.local_path:asset.status='下载中'
    attempts=[];best=None
    for candidate in variants(url):
        try:
            safe=_safe_public_url(candidate)
            # 不跟随重定向，避免重定向目标绕过URL安全检查。
            with httpx.stream('GET',safe,timeout=20,follow_redirects=False) as response:
                response.raise_for_status()
                mime=response.headers.get('content-type','').split(';')[0]
                if mime not in {'image/jpeg','image/png','image/webp'}:raise ValueError('非图片响应')
                data=bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data)>MAX_IMAGE_BYTES:raise ValueError('图片超过10MB')
            with Image.open(io.BytesIO(data)) as image:
                width,height=image.size
                if width*height>40_000_000:raise ValueError('像素过大')
                image.verify()
            attempts.append({'url':candidate,'status':'success','width':width,'height':height})
            if best is None or width*height>best[0]:best=(width*height,bytes(data),mime,width,height,candidate)
        except Exception as exc:attempts.append({'url':candidate,'status':'failed','error_type':type(exc).__name__})
    with session_scope(root) as s:
        asset=s.get(ProductAsset,asset_id)
        update_metadata(s,asset_id,download_attempts=attempts,downloaded_at=utcnow(),download_error=None if best else '本次下载失败；已有图片保留')
        if not best:asset.status=previous_status if asset.local_path else '下载失败'
        elif asset.local_path and (asset.width or 0)*(asset.height or 0)>=best[0]:
            asset.status=previous_status
        else:
            _,data,mime,width,height,source=best;digest=hashlib.sha256(data).hexdigest()
            duplicate=s.scalar(select(ProductAsset).where(ProductAsset.product_id==pid,ProductAsset.sha256==digest,ProductAsset.asset_id!=asset_id))
            if duplicate:
                # 保留旧ID作为来源别名，UI不再显示重复待下载项；已有引用不受影响。
                duplicate=resolve(s,duplicate)
                priorities={'主图':0,'SKU图':1,'详情图':2}
                if priorities.get(asset.asset_type,3)<priorities.get(duplicate.asset_type,3):duplicate.asset_type=asset.asset_type
                evidence=metadata(s,duplicate.asset_id).get('region_evidence',[])+metadata(s,asset_id).get('region_evidence',[])
                update_metadata(s,duplicate.asset_id,region_evidence=evidence)
                update_metadata(s,asset_id,canonical_asset_id=duplicate.asset_id,best_source_url=source)
                asset.status='已合并:'+duplicate.asset_id
            else:
                folder=root/'data/assets'/pid/'source';folder.mkdir(parents=True,exist_ok=True)
                path=folder/(digest+{'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp'}[mime]);path.write_bytes(data)
                if asset.sha256!=digest:asset.authorization_status='未确认';asset.cutout_path=None
                asset.local_path=str(path.relative_to(root));asset.sha256=digest;asset.mime_type=mime
                asset.byte_size=len(data);asset.width=width;asset.height=height;asset.collected_at=utcnow()
                update_metadata(s,asset_id,best_source_url=source)
                asset.status='可使用' if asset.authorization_status=='已确认' else '待授权确认'
        # 小尺寸且没有任何可靠区域分类的图片通常是图标、头像或装饰资源。
        # 只在两个条件同时成立时自动 PASS，避免误伤 256×256 的真实 SKU 图，
        # 也保留大尺寸待分类图片给人工复核。
        canonical=resolve(s,asset)
        canonical_meta=metadata(s,canonical.asset_id) if canonical else {}
        if (canonical and canonical.width and canonical.height and
                quality(canonical.width,canonical.height,canonical.asset_type)=='缩略图' and
                not is_review_image(canonical_meta)):
            update_metadata(s,canonical.asset_id,review_included=False,
                auto_passed=True,auto_pass_reason='非评论区缩略图，已过滤且不进入OCR/模型')
        status=asset.status
    return {'asset_id':asset_id,'status':status,'attempts':attempts}
