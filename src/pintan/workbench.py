"""Consumer workbench projections, isolated covers, and immutable listing packages."""
import hashlib
import io
import json
import csv
import zipfile
import re
from pathlib import Path

import httpx
from PIL import Image
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .asset_records import metadata, resolve
from .database import (Product, ProductCover, ProductAsset, ListingPackage, PlatformContent,
                       PlatformContentVersion, ContentMaster, GeneratedMedia, AssetAuthorization,
                       CollectionRun, CollectedSku, StableScore, DistributionTask,
                       session_scope, product_bundle, utcnow)
from .media_pipeline import _safe_public_url, MAX_IMAGE_BYTES, resolve_asset_file, resolve_media_file


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


PLATFORMS = {
    '淘宝': {'seller_url': 'https://item.upload.taobao.com/router/publish.htm', 'label': '千牛卖家中心'},
    '京东': {'seller_url': 'https://sell.jd.com/', 'label': '京麦商家后台'},
    '抖店': {'seller_url': 'https://fxg.jinritemai.com/ffa/g/create', 'label': '抖店商家后台'},
    '抖音': {'seller_url': 'https://fxg.jinritemai.com/ffa/g/create', 'label': '抖店商家后台'},
}


def _value(fields, *names, default=''):
    for name in names:
        value = fields.get(name)
        if value not in (None, '', [], {}): return value
    return default


def _text(value):
    if isinstance(value, list):
        return '\n'.join(_text(x) for x in value if _text(x))
    if isinstance(value, dict):
        return '\n'.join(f'{k}：{_text(v)}' for k, v in value.items())
    return str(value or '').strip()


def listing_snapshot(root, package):
    """把平台文案、SKU与图片整理成一个可导出、可辅助填写的固定结构。"""
    fields = package.get('fields') or {}
    platform = package.get('platform') if package.get('platform') in PLATFORMS else '淘宝'
    with session_scope(root) as s:
        score = s.get(StableScore, package['product_id'])
        stable = json.loads(score.payload) if score else {}
        latest = s.scalar(select(CollectionRun).where(CollectionRun.product_id == package['product_id'])
                          .order_by(CollectionRun.created_at.desc(), CollectionRun.run_id.desc()))
        skus = list(s.scalars(select(CollectedSku).where(CollectedSku.product_id == package['product_id'],
                    CollectedSku.run_id == latest.run_id).order_by(CollectedSku.sku_id))) if latest else []
    prices = stable.get('sku_reference_prices') or stable.get('ai_sku_reference_prices') or {}
    sku_rows = []
    for row in skus:
        data = json.loads(row.spec_json or '{}')
        spec = str(data.get('text') or data.get('specification') or data.get('name') or row.sku_id)
        sku_rows.append({'sku_id': row.sku_id, 'specification': spec,
                         'purchase_price_cny': row.price_cny,
                         'retail_price_cny': prices.get(spec, ''), 'stock': row.stock if row.stock is not None else ''})
    if not sku_rows:
        for item in stable.get('sku_scores') or []:
            sku_rows.append({'sku_id': '', 'specification': item.get('specification', ''),
                'purchase_price_cny': item.get('wholesale_price_cny', ''),
                'retail_price_cny': item.get('reference_retail_price_cny', ''), 'stock': ''})
    title = _text(_value(fields, '商品标题', '平台标题', '标题', default=package.get('title', '')))
    selling = _value(fields, '核心卖点', '卖点', '卖点文案', default=[])
    detail = _value(fields, '详情页模块', '详情文案', '商品描述', default=fields)
    images = sorted(package.get('media') or [], key=lambda x: (x.get('slot', ''), x.get('version', 0)))
    checks = [
        {'key': 'content', 'label': '标题与文案', 'status': 'ready' if title and _text(detail) else 'missing', 'message': '已生成' if title and _text(detail) else '缺少标题或详情文案'},
        {'key': 'images', 'label': '商品图片', 'status': 'ready' if images else 'missing', 'message': f'已选择 {len(images)} 张'},
        {'key': 'sku', 'label': 'SKU与售价', 'status': 'review' if sku_rows else 'missing', 'message': f'{len(sku_rows)} 个规格，发布前核对价格与库存' if sku_rows else '缺少可发布SKU'},
        {'key': 'category', 'label': '平台类目与属性', 'status': 'review', 'message': '须在卖家后台选择叶子类目并核对必填属性'},
        {'key': 'shipping', 'label': '物流与运费', 'status': 'review', 'message': '须选择店铺现有运费模板并确认发货承诺'},
        {'key': 'qualification', 'label': '资质与品牌', 'status': 'review', 'message': '须由商家确认类目资质、品牌及知识产权'},
    ]
    return {'adapter_version': 'listing-adapter-v1.0', 'platform': platform,
        'seller_url': PLATFORMS[platform]['seller_url'], 'seller_label': PLATFORMS[platform]['label'],
        'title': title[:120], 'short_title': _text(_value(fields, '短标题', '搜索标题', default=title))[:60],
        'selling_points': selling if isinstance(selling, list) else [_text(selling)],
        'description': _text(detail), 'attributes': _value(fields, '属性建议', '商品属性', default=[]),
        'skus': sku_rows, 'images': images, 'checklist': checks,
        'autofill': {'title': title[:120], 'description': _text(detail)[:12000]},
        'notice': '辅助填写只处理可安全识别的文本字段，不选择类目、运费模板，不上传图片，也不会点击发布。'}


def package_payload(root, row):
    data = json.loads(row.payload)
    if not data.get('listing'):
        data['listing'] = listing_snapshot(root, data)
    return data


def safe_cover_file(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to((root / 'data/covers').resolve()) or not path.is_file():
        raise ValueError('封面文件不可用')
    return path


def cover_info(root, product_id):
    with session_scope(root) as s:
        assets = list(s.scalars(select(ProductAsset).where(ProductAsset.product_id == product_id)))
        for a in assets:
            meta = metadata(s, a.asset_id)
            main = a.asset_type == '主图' or any(x.get('region') == '主图' for x in meta.get('region_evidence', []))
            if main and a.local_path and meta.get('review_included', True) and not a.status.startswith('已合并:') and a.status != '复核排除':
                try: resolve_asset_file(root, a.asset_id)
                except ValueError: continue
                return {'cover_url': f'/api/assets/{a.asset_id}/file', 'cover_status': 'available'}
        row = s.get(ProductCover, product_id)
        data = json.loads(row.payload) if row else {}
    if data.get('path'):
        try:
            safe_cover_file(root, data['path'])
            return {'cover_url': f'/api/products/{product_id}/cover', 'cover_status': 'available'}
        except ValueError: pass
    return {'cover_url': None, 'cover_status': data.get('status', 'missing')}


def product_id_for(row):
    value = row.get('product_id') or row.get('source_supply_id') or row.get('stable_id') or row.get('trend_id') or ''
    if value.startswith('ST-DAY-'): return '1688-' + value[7:]
    if value.startswith('ST-1688-'): return value[3:]
    return value


def with_covers(root, rows):
    cache = {}
    for row in rows:
        pid = product_id_for(row)
        if pid not in cache: cache[pid] = cover_info(root, pid)
        row.update(cache[pid])
    return rows


def capture_cover(root, product_id, snapshot=None):
    """At most one validated image; never invokes OCR, cutout, classification or authorization."""
    existing = cover_info(root, product_id)
    if existing['cover_url']: return existing
    with session_scope(root) as s:
        if not s.get(Product, product_id): raise ValueError('商品不存在')
        if snapshot is None:
            run = s.scalar(select(CollectionRun).where(CollectionRun.product_id == product_id)
                           .order_by(CollectionRun.created_at.desc()))
            snapshot = json.loads(run.raw_snapshot_json) if run else {}
    candidates = [x for x in snapshot.get('images', []) if isinstance(x, dict) and x.get('region') == '主图']
    url = next((x.get('url') for x in candidates if x.get('url')), None)
    data = {'status': 'missing', 'updated_at': utcnow()}
    if url:
        try:
            safe = _safe_public_url(url)
            with httpx.stream('GET', safe, timeout=12, follow_redirects=False) as response:
                response.raise_for_status()
                mime = response.headers.get('content-type', '').split(';')[0]
                if mime not in {'image/jpeg', 'image/png', 'image/webp'}: raise ValueError('不是图片')
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_IMAGE_BYTES: raise ValueError('图片过大')
            with Image.open(io.BytesIO(raw)) as im:
                if im.width * im.height > 40_000_000: raise ValueError('图片像素过大')
                im.load(); im.thumbnail((400, 400))
                buf = io.BytesIO(); im.convert('RGB').save(buf, format='JPEG', quality=85)
            digest = hashlib.sha256(raw).hexdigest()
            folder = root / 'data/covers'; folder.mkdir(parents=True, exist_ok=True)
            path = folder / (digest + '.jpg'); path.write_bytes(buf.getvalue())
            data.update(status='available', path=str(path.relative_to(root)), source_url=url, sha256=digest)
        except Exception as exc:
            data.update(status='failed', error_type=type(exc).__name__)
    with session_scope(root) as s:
        row = s.get(ProductCover, product_id)
        if row: row.payload = dumps(data)
        else: s.add(ProductCover(product_id=product_id, payload=dumps(data)))
    return cover_info(root, product_id)


def confirm_package(root, content_id, version_number, media_ids):
    if not media_ids or len(set(media_ids)) != len(media_ids): raise ValueError('请至少选择一张成功图片，且不要重复选择')
    with session_scope(root) as s:
        content = s.get(PlatformContent, content_id)
        if not content or content.current_version != version_number: raise ValueError('文案已更新，请刷新预览后确认')
        version = s.scalar(select(PlatformContentVersion).where(PlatformContentVersion.content_id == content_id,
                           PlatformContentVersion.version == version_number))
        master = s.get(ContentMaster, content.master_id)
        bundle = product_bundle(root, master.product_id)
        from .content_readiness import readiness
        if not bundle['claims'] or not readiness(root, master.product_id)['facts']['confirmed']:
            raise ValueError('请先确认商品事实')
        if not version or not json.loads(version.visible_json) or content.risk_level == '高' or json.loads(version.issues_json or '[]'):
            raise ValueError('文案存在待处理风险，请返回内容生成修改')
        selected = []
        for mid in media_ids:
            media = s.get(GeneratedMedia, mid)
            if not media or media.content_id != content_id or media.product_id != master.product_id or media.media_type != 'image' or media.status != 'success':
                raise ValueError('所选图片不属于当前内容或尚未生成成功')
            resolve_media_file(root, mid)
            meta = json.loads(media.metadata_json or '{}')
            if meta.get('content_version') != version_number: raise ValueError('图片与当前文案版本不一致，请重新选择')
            sources = list(meta.get('source_asset_ids') or ([media.source_asset_id] if media.source_asset_id else []))
            hashes = meta.get('source_hashes') or ([media.source_sha256] if media.source_sha256 else [])
            if meta.get('generation_mode') == 'text_only_concept' and not meta.get('model_input_asset_ids'):
                sources, hashes = [], []  # Facts are not photographic inputs to a pure scene.
            if not sources and meta.get('generation_mode') != 'text_only_concept': raise ValueError('图片缺少可追溯的原图信息')
            if len(hashes) != len(sources): raise ValueError('图片来源记录不完整')
            for aid, digest in zip(sources, hashes):
                raw = s.get(ProductAsset, aid); a = resolve(s, raw) if raw else None
                if not a or a.sha256 != digest or metadata(s, a.asset_id).get('review_included', True) is False or a.status == '复核排除':
                    raise ValueError('图片来源已改变或被排除，请复核')
                auth = s.scalar(select(AssetAuthorization).where(AssetAuthorization.asset_id == a.asset_id,
                    AssetAuthorization.asset_sha256 == digest, AssetAuthorization.confirmed.is_(True)))
                if not auth: raise ValueError('请先确认所选图片的原图使用权')
            selected.append({'media_id': mid, 'slot': media.slot, 'version': media.version,
                             'sha256': media.sha256, 'url': f'/api/media/files/{mid}'})
        fingerprint = hashlib.sha256(dumps([content_id, version_number, sorted(media_ids)]).encode()).hexdigest()
        old = s.scalar(select(ListingPackage).where(ListingPackage.fingerprint == fingerprint))
        if old: return json.loads(old.payload)
        product = s.get(Product, master.product_id)
        package_id = 'LP-' + fingerprint[:24]
        payload = {'package_id': package_id, 'product_id': master.product_id, 'title': product.title,
            'platform': content.platform, 'content_id': content_id, 'content_version': version_number,
            'fields': json.loads(version.visible_json), 'media': selected, 'confirmed_at': utcnow()}
        payload['listing'] = listing_snapshot(root, payload)
        s.add(ListingPackage(package_id=package_id, product_id=master.product_id, content_id=content_id,
                            fingerprint=fingerprint, payload=dumps(payload), created_at=payload['confirmed_at']))
        version.status = '已确认'
        for mid in media_ids: s.get(GeneratedMedia, mid).confirmed = True
    return payload


def install_routes(app, root, ready):
    from fastapi import Body, HTTPException, BackgroundTasks
    from fastapi.responses import FileResponse, Response

    @app.get('/api/products/{product_id}/cover')
    def cover_file(product_id: str):
        with session_scope(root) as s:
            row = s.get(ProductCover, product_id)
            data = json.loads(row.payload) if row else {}
        try: return FileResponse(safe_cover_file(root, data.get('path', '')))
        except ValueError as exc: raise HTTPException(404, str(exc)) from None

    @app.post('/api/products/{product_id}/cover', status_code=202)
    def retry_cover(product_id: str, background: BackgroundTasks):
        with session_scope(root) as s:
            if not s.get(Product, product_id): raise HTTPException(404, '商品不存在')
        background.add_task(capture_cover, root, product_id)
        return {'status': 'queued'}

    @app.get('/api/distribution/products')
    def products():
        grouped = {}
        with session_scope(root) as s:
            confirmed = list(s.scalars(select(ListingPackage).order_by(ListingPackage.created_at.desc())))
        for item in ready()['items']:
            p = item['product']; pid = p['product_id']
            row = grouped.setdefault(pid, {**p, 'platforms': [], 'image_count': 0, 'updated_at': item['created_at'],
                'package_id': next((x.package_id for x in confirmed if x.product_id == pid), None)})
            if item['platform'] not in row['platforms']: row['platforms'].append(item['platform'])
            row['image_count'] += sum(m['media_type']=='image' and bool(m.get('url')) for m in item['media'])
            row['updated_at'] = max(row['updated_at'], item['created_at'], *(m['created_at'] for m in item['media']))
        return {'items': with_covers(root, sorted(grouped.values(),key=lambda x:x['updated_at'],reverse=True))}

    @app.get('/api/distribution/products/{product_id}')
    def preview(product_id: str):
        items = [x for x in ready()['items'] if x['product']['product_id'] == product_id]
        with session_scope(root) as s:
            packages = [{'package_id': x.package_id, 'content_id': x.content_id, 'confirmed_at': x.created_at}
                for x in s.scalars(select(ListingPackage).where(ListingPackage.product_id == product_id).order_by(ListingPackage.created_at.desc()))]
        return {'items': items, 'packages': packages}

    @app.post('/api/distribution/packages')
    def confirm(payload: dict = Body(...)):
        try:
            ids = payload.get('media_ids', [])
            if not isinstance(ids, list) or not all(isinstance(x, str) for x in ids): raise ValueError('图片选择格式无效')
            return confirm_package(root, str(payload.get('content_id', '')), int(payload.get('content_version', 0)), ids)
        except (ValueError, TypeError) as exc: raise HTTPException(409, str(exc)) from None
        except IntegrityError:
            # Concurrent identical confirmations share the deterministic package identity.
            return confirm_package(root, str(payload['content_id']), int(payload['content_version']), ids)

    @app.get('/api/distribution/packages/{package_id}')
    def package(package_id: str):
        with session_scope(root) as s:
            row = s.get(ListingPackage, package_id)
            if not row: raise HTTPException(404, '内容包不存在')
            data = package_payload(root, row)
            content = s.get(PlatformContent, row.content_id)
            data['has_newer_content'] = bool(content and content.current_version != data['content_version'])
            newer = s.scalar(select(GeneratedMedia).where(GeneratedMedia.content_id == row.content_id,
                GeneratedMedia.status == 'success', GeneratedMedia.created_at > row.created_at))
            data['has_newer_content'] |= bool(newer)
        return data

    @app.get('/api/distribution/packages/{package_id}/download')
    def download_package(package_id: str):
        """下载可审计的上架包；只包含用户已经确认的版本。"""
        with session_scope(root) as s:
            row = s.get(ListingPackage, package_id)
            if not row: raise HTTPException(404, '内容包不存在')
            data = package_payload(root, row); listing = data['listing']
            files = []
            for index, item in enumerate(data.get('media') or [], 1):
                media = s.get(GeneratedMedia, item.get('media_id'))
                if not media: continue
                try: path = resolve_media_file(root, media.media_id)
                except ValueError: continue
                suffix = path.suffix.lower() if path.suffix.lower() in {'.jpg','.jpeg','.png','.webp'} else '.jpg'
                safe_slot = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff_-]+', '-', str(item.get('slot') or '图片')).strip('-')[:50]
                files.append((f'images/{index:02d}-{safe_slot}{suffix}', path))
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('上架资料.json', json.dumps({'package_id': package_id, **listing}, ensure_ascii=False, indent=2))
            archive.writestr('文案.txt', ('商品标题\n'+listing['title']+'\n\n核心卖点\n'+
                '\n'.join(listing['selling_points'])+'\n\n详情文案\n'+listing['description']).encode('utf-8-sig'))
            sku_buf = io.StringIO(newline=''); writer = csv.DictWriter(sku_buf,
                fieldnames=['sku_id','specification','purchase_price_cny','retail_price_cny','stock'])
            writer.writeheader(); writer.writerows(listing['skus'])
            archive.writestr('SKU.csv', ('\ufeff'+sku_buf.getvalue()).encode('utf-8'))
            checklist = '\n'.join(('✓ ' if x['status']=='ready' else '□ ')+x['label']+'：'+x['message'] for x in listing['checklist'])
            archive.writestr('发布前检查.txt', (checklist+'\n\n'+listing['notice']).encode('utf-8-sig'))
            for name, path in files: archive.write(path, name)
        filename = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff_-]+', '-', data.get('title','商品'))[:60] or '商品'
        return Response(output.getvalue(), media_type='application/zip', headers={
            'Content-Disposition': "attachment; filename*=UTF-8''" + __import__('urllib.parse').parse.quote(f'{filename}-{listing["platform"]}-上架包.zip')})

    @app.post('/api/distribution/packages/{package_id}/assist', status_code=202)
    async def assist(package_id: str):
        with session_scope(root) as s:
            row = s.get(ListingPackage, package_id)
            if not row: raise HTTPException(404, '内容包不存在')
            data = package_payload(root, row)
        if data['listing']['platform'] != '淘宝':
            raise HTTPException(422, '当前首版只支持淘宝/千牛辅助填写；其他平台请先下载上架包')
        return await app.state.collector_bridge.submit_distribution(package_id, data['listing'])

    @app.get('/api/distribution/tasks/{task_id}')
    def distribution_task(task_id: str):
        return app.state.collector_bridge.read_distribution(task_id)

    @app.post('/api/distribution/tasks/{task_id}/cancel')
    async def cancel_distribution(task_id: str):
        return await app.state.collector_bridge.cancel_distribution(task_id)
