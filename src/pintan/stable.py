from __future__ import annotations

import json, math, re
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select

from .data_store import STABLE_FILE, atomic_write_csv, read_csv
from .database import BusinessRow, ProductAsset, session_scope

VERSION = 'stable-rules-v5-three-pillars'

def _clamp(value): return round(min(100, max(0, float(value))) + 1e-10, 1)
def _number(value):
    try: return float(value) if value not in (None, '') else None
    except (TypeError, ValueError): return None

def _candidate_rows(root):
    with session_scope(root) as s:
        return [json.loads(x.payload) for x in s.scalars(select(BusinessRow).where(BusinessRow.kind == 'candidate'))]

def _save_candidates(root, rows):
    with session_scope(root) as s:
        existing = {x.row_id: x for x in s.scalars(select(BusinessRow).where(BusinessRow.kind == 'candidate'))}
        for row in rows:
            rid = row['stable_id']; payload = json.dumps(row, ensure_ascii=False)
            if rid in existing: existing[rid].payload = payload
            else: s.add(BusinessRow(kind='candidate', row_id=rid, payload=payload))

def _operating_fit(row, asset_count):
    text = ' '.join(str(row.get(k) or '') for k in ('title', 'category', 'material', 'feature_tags')).lower()
    subs = {}; regulated_hint = False
    if text:
        after = 82
        if any(x in text for x in ('玻璃', '陶瓷', '易碎')): after -= 30
        if any(x in text for x in ('刀', '锋利', '液体', '漏')): after -= 15
        subs['售后与破损友好度'] = _clamp(after)
        infringement = 92
        if any(x in text for x in ('专利', '正品', '同款', '联名')): infringement -= 30
        if any(x in text for x in ('迪士尼', '奥特曼', '漫威', '苹果logo')): infringement -= 55
        subs['侵权安全度'] = _clamp(infringement)
        demand = 65
        if any(x in text for x in ('清洁', '收纳', '厨房', '垃圾袋', '刷', '耗材')): demand += 20
        if any(x in text for x in ('节日', '圣诞', '季节限定')): demand -= 35
        subs['需求稳定性'] = _clamp(demand)
        regulated_hint = any(x in text for x in ('化妆品', '护肤品', '彩妆', '充电器', '电源适配器', '插座', '医疗器械'))
        # 关键词只能形成待复核风险，不能把“可能需要资质”直接当作已确认事实。
        subs['资质与上架友好度'] = 35 if regulated_hint else 90
    try: pairs = json.loads(row.get('spec_price_pairs') or '[]')
    except (TypeError, ValueError): pairs = []
    if pairs:
        supply = 55 + min(15, len(pairs) * 2)
        evidence = row.get('scoring_evidence') or {}
        if isinstance(evidence, str):
            try: evidence = json.loads(evidence)
            except ValueError: evidence = {}
        if evidence.get('shipping_proxy', {}).get('raw') == '包邮': supply += 10
        if row.get('supplier_quality_proxy_pct') not in (None, ''): supply += float(row['supplier_quality_proxy_pct']) * .2
        subs['货源履约能力'] = _clamp(supply)
    if asset_count: subs['素材完整度'] = _clamp(45 + min(55, asset_count * 3))
    hard = []
    if row.get('confirmed_requires_unsupported_qualification') is True:
        hard.append('已确认需要项目暂不支持的特殊资质')
    if row.get('confirmed_infringement') is True: hard.append('已人工确认侵权风险')
    complaints = _number(row.get('confirmed_quality_complaint_rate_pct')); samples = _number(row.get('review_sample_count'))
    if complaints is not None and samples is not None and samples >= 10 and complaints >= 30: hard.append('质量投诉比例达到淘汰阈值')
    return subs, (_clamp(sum(subs.values()) / len(subs)) if subs else ''), hard

def _score_rows(root, rows):
    with session_scope(root) as s:
        asset_counts = {r['stable_id']: len(list(s.scalars(select(ProductAsset).where(ProductAsset.product_id == r['stable_id'])))) for r in rows}
    now = datetime.now(timezone.utc).isoformat(timespec='seconds'); scored = []
    for row in rows:
        row=dict(row)
        identity=re.search(r'(?:offer/|1688-)(\d+)',str(row.get('product_url') or row.get('canonical_source_url') or row.get('stable_id') or ''))
        if identity and (row.get('source_platform')=='1688' or str(row.get('stable_id','')).startswith('1688-')):
            canonical=f'https://detail.1688.com/offer/{identity.group(1)}.html'
            row['product_url']=canonical;row['canonical_source_url']=canonical;row['offer_id']=identity.group(1)
        evidence = row.get('scoring_evidence') or {}
        if isinstance(evidence, str):
            try: evidence = json.loads(evidence)
            except ValueError: evidence = {}
        sales = _number(row.get('sales_30d'))
        if sales is None: sales = _number(evidence.get('sales_proxy', {}).get('value'))
        repurchase = _number(row.get('repurchase_rate_pct'))
        if repurchase is None: repurchase = _number(evidence.get('repurchase_proxy', {}).get('value'))
        sales_score = _clamp(20 * math.log10(sales + 1)) if sales is not None and sales >= 0 else ''
        return_score = _clamp((repurchase - 10) / 40 * 100) if repurchase is not None else ''
        market = _clamp(sales_score * .65 + return_score * .35) if sales_score != '' and return_score != '' else ''
        try: pairs = [x for x in json.loads(row.get('spec_price_pairs') or '[]') if x.get('price_cny') is not None]
        except (TypeError, ValueError): pairs = []
        prices = row.get('sku_reference_prices') or {}
        if isinstance(prices, str):
            try: prices = json.loads(prices)
            except ValueError: prices = {}
        shipping = _number(row.get('estimated_shipping_cny'))
        if shipping is None:
            shipping = 0; row['estimated_shipping_cny'] = '0'; row['shipping_assumption'] = '运费未采集，暂按0元估算'
        sku_scores = []
        for pair in pairs:
            spec = str(pair.get('text', '')).strip(); retail = _number(prices.get(spec)); profit = ''
            if retail is not None and retail > 0:
                cost = float(pair['price_cny']) + shipping; profit = _clamp(((retail - cost) / retail * 100) * 1.75)
            sku_scores.append({'specification': spec, 'wholesale_price_cny': float(pair['price_cny']),
                'reference_retail_price_cny': retail if retail is not None else '', 'profit_score': profit})
        profit = (_clamp(sum(x['profit_score'] for x in sku_scores) / len(sku_scores))
                  if sku_scores and all(x['profit_score'] != '' for x in sku_scores) else '')
        subs, operating, hard = _operating_fit(row, asset_counts.get(row['stable_id'], 0))
        missing = []
        if profit == '': missing.append('所有SKU的采购价与参考售价')
        if sales_score == '': missing.append('销量')
        if return_score == '': missing.append('商品复购率或店铺回头率')
        if operating == '': missing.append('经营适配证据')
        total = _clamp(profit * .4 + market * .3 + operating * .3) if profit != '' and market != '' and operating != '' and not hard else ''
        out = dict(row); out.update(profit_score=profit, market_validation_score=market, operating_fit_score=operating,
            operating_subscores=subs, operating_coverage=f'{len(subs)}/6', sales_score=sales_score,
            return_rate_score=return_score, total_score=total, sku_scores=sku_scores, sku_score_count=len(sku_scores),
            hard_rejections=hard, promotion_status=('eligible' if total != '' else ('rejected' if hard else 'needs_input')),
            missing_score_fields='|'.join(missing), score_status=('完整参考评分' if total != '' else ('一票否决' if hard else '待补充后评分')),
            score_coverage_pct=(40 if profit != '' else 0) + (30 if market != '' else 0) + (30 if operating != '' else 0),
            score_version=VERSION, scored_at=now, margin_score=profit, heat_score=market,
            competition_score=market, differentiation_score=operating, trust_score=operating,
            score_aggregation='利润潜力40% + 市场验证30% + 经营适配30%；利润按全部SKU平均')
        if subs.get('资质与上架友好度') == 35 and not hard:
            out['risk_review_flags']=['品类关键词可能涉及资质，仅提示人工复核，不自动淘汰']
        scored.append(out)
    return scored

def score_and_promote_candidates(root):
    candidates = _score_rows(root, _candidate_rows(root)); _save_candidates(root, candidates)
    current = read_csv(root / STABLE_FILE); rescored_old = _score_rows(root, current) if current else []
    stable = {x['stable_id']: x for x in rescored_old if x.get('total_score') not in ('', None)}
    with session_scope(root) as s:
        for row in rescored_old:
            if row.get('total_score') in ('', None) and not s.get(BusinessRow, ('candidate', row['stable_id'])):
                s.add(BusinessRow(kind='candidate', row_id=row['stable_id'], payload=json.dumps(row, ensure_ascii=False)))
        for row in candidates:
            if row.get('promotion_status') == 'eligible':
                previous = stable.get(row['stable_id'], {})
                stable[row['stable_id']] = {**previous, **row, 'verification_status': '完整评分·已入稳定池'}
                candidate = s.get(BusinessRow, ('candidate', row['stable_id']))
                if candidate: s.delete(candidate)
            elif row['stable_id'] in stable:
                stable[row['stable_id']]['pending_update_run_id'] = row.get('source_run_id', '')
                stable[row['stable_id']]['pending_update_status'] = '存在待处理更新'
    ranked = sorted(stable.values(), key=lambda x: (-float(x.get('total_score') or 0), x['stable_id']))
    for index, row in enumerate(ranked, 1): row['stable_rank'] = str(index)
    atomic_write_csv(root / STABLE_FILE, ranked)
    return ranked

def score_stable_products(root: Path): return score_and_promote_candidates(root)

def deterministic_report(rows):
    top = [r for r in rows if r.get('total_score') not in (None, '')][:3]
    if not top: return '暂无满足三项完整评分要求的稳定商品。'
    return '三维评分已完成。前三名为：' + '；'.join(f"{r.get('stable_rank')}. {r['title']}（{r['total_score']}分）" for r in top) + '。'
