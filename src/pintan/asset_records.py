"""采集、下载共用的素材身份与来源管理。"""
import json
from sqlalchemy import select
from .database import AssetCaptureMeta, ProductAsset

def metadata(s,aid):
    row=s.get(AssetCaptureMeta,aid)
    return json.loads(row.payload) if row else {}

def update_metadata(s,aid,**values):
    data=metadata(s,aid);data.update(values)
    row=s.get(AssetCaptureMeta,aid)
    if row:row.payload=json.dumps(data,ensure_ascii=False)
    else:s.add(AssetCaptureMeta(asset_id=aid,payload=json.dumps(data,ensure_ascii=False)))

def resolve(s,asset):
    seen=set()
    while asset and asset.status.startswith('已合并:'):
        if asset.asset_id in seen:raise ValueError('素材别名循环')
        seen.add(asset.asset_id);asset=s.get(ProductAsset,asset.status.split(':',1)[1])
    return asset

def find_source(s,pid,url):
    rows=list(s.scalars(select(ProductAsset).where(ProductAsset.product_id==pid,ProductAsset.source_url==url)))
    rows.sort(key=lambda a:(not bool(a.local_path),a.asset_id))
    return resolve(s,rows[0]) if rows else None

def reconcile(s,pid):
    """仅合并同URL或相同哈希，保留所有原记录与文件。"""
    rows=list(s.scalars(select(ProductAsset).where(ProductAsset.product_id==pid)))
    rows.sort(key=lambda a:(not bool(a.local_path),-(a.width or 0)*(a.height or 0),a.asset_id))
    urls={};hashes={};merged=0
    for a in rows:
        if not a.source_url or a.status.startswith('已合并:'):continue
        target=urls.get(a.source_url) or (hashes.get(a.sha256) if a.sha256 else None)
        if target:
            # 历史分类仅作为线索，明确主图标签优先；不推断功能/参数/场景。
            if a.asset_type=='主图' and target.asset_type in {'原图','商品图','待分类商品图'}:target.asset_type='主图'
            update_metadata(s,target.asset_id,legacy_classification=True)
            a.status='已合并:'+target.asset_id;update_metadata(s,a.asset_id,canonical_asset_id=target.asset_id);merged+=1
        else:target=a
        urls[a.source_url]=target
        if a.sha256:hashes[a.sha256]=target
    return merged
