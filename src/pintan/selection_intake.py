"""已复核采集记录进入稳定选品；缺失经营数据保留空值。"""
import json
import math
from .database import BusinessRow
from .collector_mapping import read_mapping
from .scoring_evidence import extract_signals

def enroll_stable(session, run, kind='stable'):
    mapping=read_mapping(session,run.run_id)
    fields=[f for f in mapping['review_fields'] if f.get('included')]
    values={f['field_name']:f['value'] for f in fields}
    row=session.get(BusinessRow,{'kind':kind,'row_id':run.product_id})
    prior=json.loads(row.payload) if row else {}
    if kind=='candidate' and not prior:
        promoted=session.get(BusinessRow,{'kind':'stable','row_id':run.product_id})
        if promoted:prior=json.loads(promoted.payload)
    pairs=mapping.get('spec_price_pairs',[])
    # 没有采购规格选择时，不用最低价代表整个商品。
    selected=next((x for x in pairs if x.get('text')==prior.get('selected_specification')),None)
    if len(pairs)==1:selected=pairs[0]
    canonical_url=f'https://detail.1688.com/offer/{run.offer_id}.html'
    data={**prior,'stable_id':run.product_id,'source_supply_id':run.product_id,
          'source_run_id':run.run_id,'product_url':canonical_url,'source_platform':'1688',
          'canonical_source_url':canonical_url,'offer_id':run.offer_id,
          'title':values.get('title',''),'category':values.get('category',''),
          'material':values.get('material',''),'moq':values.get('minimum_order_text',''),
          'feature_tags':'|'.join(f['value'] for f in fields if f['field_name']=='product_features'),
          'spec_price_pairs':json.dumps(pairs,ensure_ascii=False),'verification_status':'已人工复核',
          'selected_specification':selected['text'] if selected else '',
          'wholesale_price_cny':str(selected['price_cny']) if selected and selected.get('price_cny') is not None else ''}
    for key in ('sales_30d','repurchase_rate_pct','supplier_years'):
        data[key]=str(mapping.get('numeric',{}).get(key) if mapping.get('numeric',{}).get(key) is not None else '')
    for key in ('estimated_shipping_cny','reference_retail_price_cny','supplier_quality_proxy_pct'):
        data.setdefault(key,'')
    snapshot=json.loads(run.raw_snapshot_json)
    evidence='\n'.join(str(b.get('text','')) for b in snapshot.get('text_blocks',[]))+'\n'+snapshot.get('visible_text','')
    data['scoring_evidence']=extract_signals(evidence)
    data['supplier_evidence']=[f for f in fields if f['field_name'] in {'supplier_name','supplier_years','supplier_metrics'}]
    if data['estimated_shipping_cny']=='' and 'shipping_proxy' in data['scoring_evidence']:
        data['estimated_shipping_cny']='0'
    elif data['estimated_shipping_cny']=='':
        data['estimated_shipping_cny']='0'
        data['scoring_evidence']['shipping_proxy']={'value':0,'raw':'未采集到采购运费',
            'source':'演示默认0元（待核实）','verification_status':'待复核'}
    for key in prior.get('manual_scoring_fields',[]):
        if key in prior:data[key]=prior[key]
    if row:row.payload=json.dumps(data,ensure_ascii=False)
    else:session.add(BusinessRow(kind=kind,row_id=run.product_id,payload=json.dumps(data,ensure_ascii=False)))

def enroll_candidate(session,run):
    """采集结果先进入待评分候选；只有完整评分后才晋级 stable。"""
    return enroll_stable(session,run,kind='candidate')

def update_scoring_inputs(root, product_id, payload):
    """仅接受人工填写的评分输入；采购价必须来自所选规格，不能由模型猜测。"""
    from .database import session_scope
    from .stable import score_stable_products
    allowed={'estimated_shipping_cny','reference_retail_price_cny','sales_30d',
             'repurchase_rate_pct','supplier_quality_proxy_pct','feature_tags','selected_specification','sku_reference_prices'}
    if set(payload)-allowed:raise ValueError('包含不可修改字段')
    with session_scope(root) as session:
        row=session.get(BusinessRow,{'kind':'candidate','row_id':product_id}) or session.get(BusinessRow,{'kind':'stable','row_id':product_id})
        if not row:raise ValueError('选品商品不存在')
        data=json.loads(row.payload)
        original=dict(data)
        for key,value in payload.items():
            if key in ('feature_tags','selected_specification','sku_reference_prices'):continue
            if value in ('',None):data[key]='';continue
            number=float(value)
            if not math.isfinite(number) or number<0 or (key.endswith('_pct') and number>100):raise ValueError('数值超出范围：'+key)
            data[key]=str(number)
        if 'feature_tags' in payload:data['feature_tags']=str(payload['feature_tags']).strip()
        if 'selected_specification' in payload:
            spec=next((x for x in json.loads(data.get('spec_price_pairs') or '[]') if x.get('text')==payload['selected_specification']),None)
            if not spec or spec.get('price_cny') is None:raise ValueError('请选择有明确单价的规格')
            data['selected_specification']=spec['text'];data['wholesale_price_cny']=str(spec['price_cny'])
        if 'sku_reference_prices' in payload:
            if not isinstance(payload['sku_reference_prices'],dict):raise ValueError('SKU参考价必须是规格到价格的对象')
            pairs=json.loads(data.get('spec_price_pairs') or '[]');known={str(x.get('text','')) for x in pairs if x.get('price_cny') is not None}
            if set(payload['sku_reference_prices'])-known:raise ValueError('SKU参考价包含未采集的规格')
            manual_prices=dict(data.get('manual_sku_reference_prices') or {})
            for spec,value in payload['sku_reference_prices'].items():
                number=float(value)
                if not math.isfinite(number) or number<=0:raise ValueError('SKU参考价必须大于0')
                manual_prices[spec]=number
            data['manual_sku_reference_prices']=manual_prices
            data['sku_reference_prices']={**(data.get('sku_reference_prices') or {}),**manual_prices}
        manual=set(data.get('manual_scoring_fields',[]))
        for key,value in payload.items():
            old=original.get(key,'');new=data.get(key,'')
            try:unchanged=float(old)==float(new)
            except (ValueError,TypeError):unchanged=str(old or '')==str(new or '')
            if not unchanged:manual.add(key)
        data['manual_scoring_fields']=sorted(manual)
        data['scoring_input_source']='人工填写评分参考值，非平台验证指标'
        row.payload=json.dumps(data,ensure_ascii=False)
    score_stable_products(root)
    # 商品可能已晋级 stable，也可能因销量/回头率等缺项继续留在
    # candidate。调用方必须始终拿到刚编辑的当前版本。
    with session_scope(root) as session:
        current=(session.get(BusinessRow,{'kind':'stable','row_id':product_id}) or
                 session.get(BusinessRow,{'kind':'candidate','row_id':product_id}))
        return [json.loads(current.payload)] if current else []
