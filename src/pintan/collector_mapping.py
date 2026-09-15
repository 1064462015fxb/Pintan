"""采集证据 → 标准字段 → 下游输入。

解析只接受明确标签和值，缺失保留 None；采购价格必须带适用数量和单位。
页面宣传与评价只是证据候选，不自动等同于已核验的功效或真实销量。
所有标准表按 run_id 保存，旧快照也可确定性回填，不调用付费模型。
"""
import hashlib
import json
import re
import unicodedata
from sqlalchemy import select
from .database import (OfferSnapshot, OfferMappingHistory, FactEvidence, OfferField, OfferPriceTier,
    SupplierSnapshot, PainPoint, AssetRequirement, CollectedFact, CollectedSku, ProductClaim,
    CollectionRun, Product, session_scope)

VERSION = 'collector-mapping-v2.3.0-sku-images'
REGIONS = {'title','price','sku','attributes','packaging','supplier','reviews','detail','legacy'}
# 此处同时记录消费者和理由，工作台直接使用同一目录，避免文档与实现分叉。
CATALOG = {
    'title': ('商品标题','共同必需','商品身份与文案主体'),
    'category': ('类目','选品必需','限定同类候选池，避免跨类比较'),
    'minimum_order_text': ('起批量','选品必需','核验采购约束'),
    'material': ('材质','按类目需要','商品匹配与文案规格'),
    'product_features': ('商品特征','按类目扩展','颜色、尺寸、容量、功率、包装、功能等以“名称：值”保存'),
    'supplier_name': ('供应商名称','来源必需','商品与店铺标题分离'),
    'shipping_origin': ('发货地','物流需要','运费及供应链核验'),
    'supplier_years': ('经营年限','供应商证据','只能使用页面明确标注的年限'),
    'supplier_metrics': ('供应商页面指标','供应商证据','原始指标不等同于质量代理分'),
    'supplier_services': ('商家服务','商家信息','发货、运费与售后承诺按页面口径保存，需复核适用条件'),
    'promotion_terms': ('限时活动','时效信息','促销只作为带采集时间的页面证据，不覆盖SKU采购价'),
    'customization': ('定制支持','可选','有定制需求时使用'),
    'sales_display': ('销量展示','候选信号','必须保留时间窗口、单位和区间'),
    'sales_30d': ('近30天成交','稳定热度需要','仅接受明确30天口径的确切计数'),
    'repurchase_rate_pct': ('复购率','稳定热度需要','百分比与页面口径需复核'),
    'delivery_time': ('发货时效','可选','供应条件，不能改写为无条件承诺'),
}
ALIASES = {
    '商品名称':'title','产品名称':'title','标题':'title','类目':'category','产品类别':'category',
    '起批量':'minimum_order_text','起订量':'minimum_order_text',
    '材质':'material','主要材质':'material','规格':'product_features','型号':'product_features',
    '功能':'product_features','产品功能':'product_features','尺寸':'product_features','产品尺寸':'product_features',
    '颜色':'product_features','颜色分类':'product_features','包装数量':'product_features','装箱数量':'product_features',
    '重量':'product_features','单重':'product_features','电池容量':'product_features','额定功率':'product_features','功率':'product_features',
    '适用场景':'product_features','使用场景':'product_features','使用方法':'product_features','注意事项':'product_features',
    '供应商':'supplier_name','公司名称':'supplier_name','发货地':'shipping_origin','发货地址':'shipping_origin',
    '经营年限':'supplier_years','诚信通年限':'supplier_years','入驻':'supplier_years','入驻年限':'supplier_years',
    '响应率':'supplier_metrics','发货率':'supplier_metrics','品质达标率':'supplier_metrics',
    '代发品质达标率':'supplier_metrics','48小时支撑率':'supplier_metrics','店铺回头率':'supplier_metrics',
    '24h揽收率':'supplier_metrics','48h揽收率':'supplier_metrics','商家代发热度':'supplier_metrics',
    '近7天代发数量':'supplier_metrics','近30天代发数量':'supplier_metrics','下游铺货数':'supplier_metrics',
    '铺货分销商数':'supplier_metrics','商家榜排名':'supplier_metrics','主营':'supplier_metrics',
    '定制':'customization','支持定制':'customization','销量':'sales_display','已售':'sales_display','成交':'sales_display',
    '近30天成交':'sales_30d','近30天销量':'sales_30d','30天销量':'sales_30d',
    '复购率':'repurchase_rate_pct','回头率':'repurchase_rate_pct','发货时效':'delivery_time',
}
CONTENT_FIELDS = {'title','category','material','product_features'}
COMMON_FIELDS = {'title','category','minimum_order_text','material'}
SUPPLIER_FIELDS = {'supplier_name','shipping_origin','supplier_years','supplier_metrics',
                   'supplier_services','promotion_terms','customization','delivery_time',
                   'sales_display','sales_30d','repurchase_rate_pct'}

def _review_value_key(field_name, value):
    """生成复核去重键。

    网页同一属性经常被桌面端、吸顶栏和隐藏SKU模板重复渲染。这里仅合并
    “同字段、同语义值”：统一全半角、空白和常见列表分隔符，但绝不合并
    同字段的不同值，避免把价格阶梯或真实字段冲突误删。
    """
    text=unicodedata.normalize('NFKC',str(value or '')).strip()
    text=re.sub(r'\s+','',text)
    if field_name in {'product_features','supplier_metrics'}:
        text=re.sub(r'[,，、;；|/]+',',',text).strip(',')
    return field_name,text.casefold()

def _dedupe_review_items(items):
    """合并重复复核行，并保留所有原始证据ID供审计。"""
    result=[];positions={}
    for raw in items:
        item=dict(raw)
        key=_review_value_key(item.get('field_name'),item.get('value'))
        if key not in positions:
            evidence_ids=[]
            for eid in item.get('evidence_ids') or [item.get('evidence_id')]:
                if eid and eid not in evidence_ids:evidence_ids.append(eid)
            item['evidence_ids']=evidence_ids
            item['duplicate_count']=max(1,int(item.get('duplicate_count') or 1))
            positions[key]=len(result);result.append(item);continue
        target=result[positions[key]]
        target['included']=bool(target.get('included') or item.get('included'))
        for eid in item.get('evidence_ids') or [item.get('evidence_id')]:
            if eid and eid not in target['evidence_ids']:target['evidence_ids'].append(eid)
        target['duplicate_count']=int(target.get('duplicate_count') or 1)+int(item.get('duplicate_count') or 1)
    return result

def detect_review_anomalies(items):
    """识别高确定性的字段错位；只标记，不把模糊业务判断当错误。"""
    problems=[]
    generic_titles={'商品评价','商品详情','商品属性','包装信息','规格参数','产品参数','价格说明','采购须知','店铺信息','公司介绍'}
    for item in items:
        if not item.get('included'):continue
        name=item.get('field_name');value=str(item.get('value') or '').strip();reason=''
        if name=='title' and (value in generic_titles or re.search(r'(?:有限公司|旗舰店|专营店)$',value)):
            reason='栏目或店铺名称被误识别为商品标题'
        elif name=='color_options' and re.search(r'\d+\s*(?:只|个|件|包|枚|张|套|卷)(?:装)?',value):
            reason='颜色字段混入包装数量或SKU规格'
        elif name=='material' and re.fullmatch(r'\d+\s*(?:只|个|件|包|枚|张|套|卷)(?:装)?',value):
            reason='材质字段实际为数量规格'
        if reason:problems.append({'review_id':item.get('review_id'),'field_name':name,'value':value,'reason':reason})
    return problems

def review_fields(data):
    """返回复核工作区；原始字段保持不变，人工操作作为单独草稿覆盖层。"""
    draft=data.get('review_draft',{}).get('fields')
    if isinstance(draft,list):return _dedupe_review_items(draft)
    return _dedupe_review_items([dict(review_id=f['field_id'],field_id=f['field_id'],field_name=f['field_name'],
                 value=f['value'],evidence_id=f['evidence_id'],included=f['status']!='字段冲突',
                 source_kind='页面采集',status=f['status']) for f in data.get('fields',[])])

def save_review(s,run_id,items):
    """保存人工复核草稿；删除等于排除，原始证据与字段不会物理删除。"""
    run=s.get(CollectionRun,run_id);record=s.get(OfferSnapshot,run_id)
    if not run or not record:raise ValueError('采集映射不存在')
    if run.status=='已人工复核':raise ValueError('这次采集已经确认，不能覆盖；请重新采集生成新版本')
    if not isinstance(items,list) or len(items)>100:raise ValueError('复核字段列表无效或超过100项')
    data=json.loads(record.payload);original={x['field_id']:x for x in data.get('fields',[])};clean=[]
    for index,item in enumerate(items):
        if not isinstance(item,dict):raise ValueError('复核字段格式无效')
        field_id=str(item.get('field_id') or '')
        field_name=str(item.get('field_name') or '').strip()
        value=str(item.get('value') or '').strip()
        included=bool(item.get('included',True))
        if field_name not in CATALOG:raise ValueError('复核字段不在允许目录中: '+field_name)
        if not value or len(value)>500:raise ValueError('复核值为空或超过500字')
        if field_id:
            source=original.get(field_id)
            if not source or source['field_name']!=field_name:raise ValueError('原始字段关联无效')
            evidence_id=source['evidence_id'];source_kind='页面采集' if value==source['value'] else '人工修改'
            review_id=field_id
        else:
            evidence_id=None
            # 只有专用补全接口会生成 AI 推测；即使客户端保留此标记，结果也
            # 仍处于复核草稿，最终确认时继续保留“推测”血缘。
            source_kind='AI推测' if item.get('source_kind')=='AI推测' else '人工补录'
            review_id=uid('RV-',run_id,index,field_name,value)
        confidence=float(item.get('confidence',.5 if source_kind=='AI推测' else 1.0))
        if not 0<=confidence<=1:raise ValueError('复核置信度必须在0到1之间')
        clean.append(dict(review_id=review_id,field_id=field_id or None,field_name=field_name,
                          value=value,evidence_id=evidence_id,included=included,
                          source_kind=source_kind,status='人工复核草稿',confidence=confidence,
                          rationale=str(item.get('rationale') or '')[:300],
                          based_on_field_ids=[str(x) for x in (item.get('based_on_field_ids') or [])][:10],
                          risk_note=str(item.get('risk_note') or '')[:200]))
    clean=_dedupe_review_items(clean)
    data['review_draft']={'fields':clean,'status':'草稿'}
    record.payload=json.dumps(data,ensure_ascii=False)
    return clean

def uid(prefix, *parts):
    return prefix+hashlib.sha256('|'.join(str(p) for p in parts).encode()).hexdigest()[:24]

def clean_blocks(snapshot):
    """白名单页面证据，禁用任意嵌套对象；限制单块及总量。"""
    blocks=[];remaining=80000
    for item in (snapshot.get('text_blocks') or [])[:100]:
        if not isinstance(item,dict) or item.get('region') not in REGIONS:continue
        raw=str(item.get('text') or '').strip()[:8000][:remaining]
        if not raw:continue
        remaining-=len(raw)
        blocks.append({'region':item['region'],'text':raw,
                       'source_locator':str(item.get('source_locator') or '')[:500]})
        if remaining<=0:break
    return blocks

def _join_split_decimal_prices(text):
    """还原被前端样式拆成多个文本节点的价格。

    1688新版价格常被读取为 ``¥\n4\n.83``。这里只在货币符号、整数和
    小数部分明确相邻时合并，避免把普通段落中的数字错误拼接。
    """
    return re.sub(r'([¥￥])\s*(\d+)\s*\.\s*(\d{1,4})',r'\1\2.\3',text)

def parse_mapping(snapshot, run_id):
    blocks=clean_blocks(snapshot)
    # 旧扩展仍可用，但来源只到整页，前端会显示需要新扩展重新采集。
    legacy=not blocks
    if legacy:
        if snapshot.get('title'):blocks.append(dict(region='title',text=snapshot['title'],source_locator='page.title'))
        if snapshot.get('visible_text'):blocks.append(dict(region='legacy',text=snapshot['visible_text'],source_locator='page.visible_text'))
    evidence=[];fields=[];tiers=[];reviews=[];skus=[]
    def add(name,value,eid):
        value=value.strip()
        # 页面控制文字不是属性值。物理尺寸必须有数字与单位；例如1688
        # 某些SKU把“充电款”放在“尺寸”选项下，不能据此生成尺寸文案。
        if re.match(r'^(?:展开|收起|请选择|选择规格|加入购物车|领取优惠券|已售罄)',value):return
        if name=='dimensions' and not (re.search(r'\d',value) and re.search(r'cm|mm|米|厘米|英寸|inch|[×x*]',value,re.I)):return
        if value and not any(f['field_name']==name and f['value']==value for f in fields):
            fields.append(dict(field_id=uid('OF-',run_id,name,value,eid),field_name=name,value=value,
                evidence_id=eid,status='待复核',derivation_rule='页面标签映射；保留原始单位'))
    for index,block in enumerate(blocks):
        eid=uid('EV-',run_id,index,block['region'],block['text'])
        evidence.append(dict(evidence_id=eid,**block))
        raw=_join_split_decimal_prices(block['text']);region=block['region']
        if region=='supplier':raw=raw.replace('\t','\n')
        sold=re.search(r'已售\s*\d+(?:\.\d+)?\s*万?\s*\+?\s*(?:件|包|个|笔)',raw)
        if sold:add('sales_display',sold.group(),eid)
        if region=='title':
            # 已出现的店铺公司标题不能再作为商品名称；保留为来源候选。
            if re.search(r'(?:有限公司|有限责任公司|旗舰店|专营店)$',raw.strip()):add('supplier_name',raw,eid)
            else:add('title',re.sub(r'\s*[-–—]\s*(阿里巴巴|1688).*$', '',raw),eid)
        lines=[v.strip() for v in raw.splitlines() if v.strip()]
        if region=='supplier':
            for line in lines:
                if len(line)<100 and re.fullmatch(r'[^:：]+(?:有限公司|有限责任公司)',line):
                    add('supplier_name',line,eid)
        for i,line in enumerate(lines if region!='reviews' else []):
            pair=re.match(r'^([^:：]{1,20})\s*[:：]\s*(.+)$',line)
            if not pair:
                # 兼容属性表渲染为“材质 PE”，但只对明确白名单标签生效。
                pair=re.match(r'^('+ '|'.join(re.escape(k) for k in sorted(ALIASES,key=len,reverse=True)) + r')\s+(.+)$',line)
            label,value=(pair.group(1).strip(),pair.group(2)) if pair else (line,lines[i+1] if i+1<len(lines) else '')
            if label in ALIASES and value not in ALIASES:
                target=ALIASES[label]
                if label in {'尺寸','产品尺寸'} and not (re.search(r'\d',value) and re.search(r'cm|mm|米|厘米|英寸|inch|[×x*]',value,re.I)):
                    continue
                # 可变特征和商家指标必须保留标签，否则“30只”“96%”脱离
                # 上下文后无法判断它分别代表包装数量还是揽收率。
                add(target,f'{label}：{value}' if target in {'product_features','supplier_metrics'} else value,eid)
        # 只有专用价格区（或旧整页带货币符号）才提取展示价格。
        if region in {'price','legacy'}:
            prices=list(re.finditer(r'[¥￥]\s*(\d+(?:\.\d+)?(?:\s*[-~～–]\s*\d+(?:\.\d+)?)?)',raw))
            if not prices and region=='price':
                prices=list(re.finditer(r'(\d+(?:\.\d+)?(?:\s*[-~～–]\s*\d+(?:\.\d+)?)?)\s*元',raw))
            moq=re.search(r'\d+\s*(?:件|个|包|套|箱)\s*起批',raw)
            if moq:add('minimum_order_text',moq.group(),eid)
        if region=='price':
            # 要求同一行包含数量条件、单位和单价；不把跨SKU价格范围猜成阶梯。
            for line in lines:
                match=re.fullmatch(r'(\d+)\s*(?:[-~～至]\s*(\d+))?\s*(件|个|包|套|箱)\s*(起批|以上|起)?\s*[:：]?\s*[¥￥]\s*(\d+(?:\.\d+)?)',line)
                if not match:continue
                lo,hi,unit,qualifier,price=match.groups()
                if not hi and not qualifier:continue
                if hi and int(hi)<int(lo):continue
                tiers.append(dict(tier_id=uid('PT-',run_id,line,eid),evidence_id=eid,
                    min_quantity=int(lo),max_quantity=int(hi) if hi else None,unit=unit,
                    price_cny=float(price),currency='CNY',sku_id=None,scope='页面展示，SKU适用范围待复核',status='待复核'))
        if region=='sku':
            # 新版页面可能不暴露可点击的role=option，但SKU区仍按
            # “名称 → ¥价格 → 库存”连续输出。只接受这一明确相邻关系。
            sku_lines=[x.strip() for x in raw.splitlines() if x.strip()]
            for i,line in enumerate(sku_lines):
                price_match=re.fullmatch(r'[¥￥]\s*(\d+(?:\.\d+)?)\s*(?:元)?',line)
                if not price_match or i==0:continue
                name=sku_lines[i-1]
                if name in {'颜色','规格','型号','尺寸','价格','库存'} or len(name)>300:continue
                stock=None
                if i+1<len(sku_lines):
                    stock_match=re.fullmatch(r'库存\s*[:：]?\s*(\d+)\s*(?:件|个|包|套|箱)?',sku_lines[i+1])
                    if stock_match:stock=int(stock_match.group(1))
                skus.append(dict(evidence_id=eid,text=name,price_cny=float(price_match.group(1)),stock=stock,
                    source_locator=block.get('source_locator',''),scope='SKU区域明确相邻的名称、价格与库存；组合关系待人工复核'))
        if region=='reviews':
            for line in lines[:30]:
                if len(line)>5:reviews.append(dict(pain_id=uid('PA-',run_id,line,eid),text=line,evidence_id=eid,status='待复核候选'))
        if region=='supplier':
            years=re.search(r'入驻\s*(\d+\s*年)',raw)
            if years:add('supplier_years',years.group(1),eid)
            # “浙江金华 送至 云南保山”中前者是发货地，后者是当前用户的
            # 收货上下文；只保存前者，绝不把目的地写成商家属性。
            origin=re.search(r'([\u4e00-\u9fff]{2,12})\s+送至\s+[\u4e00-\u9fff]{2,12}',raw)
            if origin:add('shipping_origin',origin.group(1),eid)
            delivery=re.search(r'承诺\s*(\d+\s*小时)\s*发货',raw)
            if delivery:add('delivery_time',delivery.group(1),eid)
            for phrase in ('包邮','敢赔付','品质不符包赔','严选晚发必赔','退货包运费','7天无理由退货'):
                if phrase in raw:
                    value='包邮（页面当前收货地及活动条件待复核）' if phrase=='包邮' else phrase
                    add('supplier_services',value,eid)
            promotion_patterns=(r'限时\s*\d+(?:\.\d+)?\s*折',r'首单减\s*\d+(?:\.\d+)?\s*元(?:且?免运费)?',
                                r'先采后付\s*\d+(?:\.\d+)?\s*元下单')
            for pattern in promotion_patterns:
                for match in re.finditer(pattern,raw):add('promotion_terms',match.group(),eid)
            # 新版商家卡常把多个指标渲染在同一行，因此不能只依赖冒号分行。
            metric_labels=('主营','品质达标率','代发品质达标率','48小时支撑率','48小时支撑','店铺回头率',
                '24h揽收率','48h揽收率','商家代发热度','近7天代发数量','近30天代发数量',
                '下游铺货数','铺货分销商数','商家榜排名')
            boundary='|'.join(re.escape(x) for x in sorted(metric_labels,key=len,reverse=True))
            for match in re.finditer(rf'({boundary})\s*[:：]?\s*([^\n]{{1,40}}?)(?=\s+(?:{boundary})\s*[:：]?|\n|$)',raw,re.I):
                label,value=match.group(1),match.group(2).strip()
                if value:add('supplier_metrics',f'{label}：{value}',eid)
    for index,item in enumerate((snapshot.get('skus') or [])[:100]):
        if not isinstance(item,dict):continue
        raw=str(item.get('text') or '').strip()[:1000]
        if not raw:continue
        eid=uid('EV-',run_id,'sku',index,raw)
        evidence.append(dict(evidence_id=eid,region='sku',text=raw,
                             source_locator=str(item.get('source_locator') or 'page.skus')[:500]))
        price=None;stock=None
        for line in raw.splitlines():
            m=re.fullmatch(r'(?:单价|价格)\s*[:：]\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*(?:元)?',line.strip())
            if m:price=float(m.group(1))
            m=re.fullmatch(r'库存\s*[:：]\s*(\d+)\s*(?:件|个|包)?',line.strip())
            if m:stock=int(m.group(1))
        skus.append(dict(evidence_id=eid,text=raw,price_cny=price,stock=stock,
                         image_url=str(item.get('image_url') or '')[:2000],dimension=str(item.get('dimension') or '')[:100],
                         image_relation_type=str(item.get('image_relation_type') or '')[:100],
                         displayed_image_changed=item.get('displayed_image_changed'),
                         source_locator=item.get('source_locator',''),scope='页面单个选项；不是自动推导的完整SKU组合'))
    skus=list({(x['text'],x.get('price_cny'),x.get('stock'),x.get('dimension','')):x for x in skus}.values())
    # 1688经常把包装规格挂在名为“颜色”的SKU维度下。若某个颜色/规格
    # 特征的全部取值已经作为带价格SKU存在，就只保留规格—单价元组，避免
    # 同一数据再次以孤立商品特征展示。
    sku_names={str(x.get('text') or '').strip() for x in skus}
    kept_fields=[]
    for field in fields:
        if field['field_name']!='product_features':kept_fields.append(field);continue
        label,_,raw_value=field['value'].partition('：')
        values=[x.strip() for x in re.split(r'[,，、]',raw_value) if x.strip()]
        duplicated=label in {'颜色','颜色分类','规格','型号'} and values and all(x in sku_names for x in values)
        if raw_value.strip() and not duplicated:kept_fields.append(field)
    fields=kept_fields
    # 同字段多值为冲突，型号/颜色等集合字段允许保留多项。
    conflicts=[]
    for name in CATALOG:
        values=sorted({f['value'] for f in fields if f['field_name']==name})
        if len(values)>1 and name not in {'product_features','supplier_metrics'}:
            conflicts.append(dict(field=name,values=values,status='待人工确认'))
            for f in fields:
                if f['field_name']==name:f['status']='字段冲突'
    resolved={name:next((f['value'] for f in fields if f['field_name']==name and f['status']!='字段冲突'),None) for name in CATALOG}
    # 标准数值只在明确计量单位与口径下生成；未知值绝不变成0。
    numeric={}
    for name,pattern in {'sales_30d':r'(\d+)\s*(?:件|个|笔)?',
        'repurchase_rate_pct':r'(\d+(?:\.\d+)?)\s*%', 'supplier_years':r'(\d+)\s*年'}.items():
        match=re.fullmatch(pattern,resolved.get(name) or '',re.I)
        numeric[name]=float(match.group(1)) if match else None
        if name=='repurchase_rate_pct' and numeric[name] is not None and numeric[name]>100:numeric[name]=None
    common=[f for f in fields if f['field_name'] in COMMON_FIELDS]
    features=[f for f in fields if f['field_name']=='product_features']
    supplier=[f for f in fields if f['field_name'] in SUPPLIER_FIELDS]
    missing=sorted(k for k in COMMON_FIELDS if resolved.get(k) in (None,''))
    if not any(f['status']!='字段冲突' for f in features):missing.append('product_features')
    if not skus:missing.append('spec_price_pairs')
    # 这些值要通过采购情境/人工输入或已定义算法给出，不能从页面最低价反推。
    external=['wholesale_price_cny（选择SKU、数量及单位后计算）','estimated_shipping_cny（按目的地取得）',
              'reference_retail_price_cny（用户或市场参考）','supplier_quality_proxy_pct（独立版本化算法）']
    content=[dict(field_id=f['field_id'],field_name=f['field_name'],value=f['value'],evidence_id=f['evidence_id'],
                  usage_status='证据冲突，禁止引用' if f['status']=='字段冲突' else '待人工复核',
                  claim_id=None) for f in fields if f['field_name'] in CONTENT_FIELDS]
    return dict(mapping_version=VERSION,legacy_snapshot=legacy,evidence=evidence,fields=fields,skus=skus,
        common_attributes=common,product_features=features,supplier_info=supplier,spec_price_pairs=skus,
        resolved=resolved,numeric=numeric,price_tiers=list({x['tier_id']:x for x in tiers}.values()),
        review_candidates=list({x['pain_id']:x for x in reviews}.values()),conflicts=conflicts,content_claims=content,
        selection_input=dict(values={**resolved,**numeric},ready=False,missing_fields=missing,
                             missing_context=external,reason='确认规格、采购数量、运费及参考售价后才能进入完整评分'),
        catalog=[dict(field_name=k,label=v[0],requirement=v[1],reason=v[2],present=bool(resolved[k])) for k,v in CATALOG.items()])

def persist_mapping(s, run, snapshot):
    old=s.get(OfferSnapshot,run.run_id)
    if old and old.mapping_version==VERSION:return json.loads(old.payload)
    result=parse_mapping(snapshot,run.run_id)
    if old:
        previous_payload=json.loads(old.payload)
        if 'review_draft' in previous_payload:result['review_draft']=previous_payload['review_draft']
        if not s.get(OfferMappingHistory,(run.run_id,old.mapping_version)):
            s.add(OfferMappingHistory(run_id=run.run_id,mapping_version=old.mapping_version,payload=old.payload))
        old.mapping_version=VERSION;old.payload=json.dumps(result,ensure_ascii=False)
        for f in s.scalars(select(OfferField).where(OfferField.run_id==run.run_id)):f.status='历史映射'
    else:
        s.add(OfferSnapshot(run_id=run.run_id,product_id=run.product_id,mapping_version=VERSION,
                            payload=json.dumps(result,ensure_ascii=False)))
    s.flush()
    for e in result['evidence']:
        if not s.get(FactEvidence,e['evidence_id']):
            s.add(FactEvidence(evidence_id=e['evidence_id'],run_id=run.run_id,region=e['region'],source_locator=e['source_locator'],raw_text=e['text']))
    s.flush()
    for f in result['fields']:
        previous=s.get(OfferField,f['field_id'])
        if previous:previous.status=f['status']
        else:s.add(OfferField(run_id=run.run_id,**{k:f[k] for k in ('field_id','field_name','value','evidence_id','status')}))
    for tier in result['price_tiers']:
        if not s.get(OfferPriceTier,tier['tier_id']):
            s.add(OfferPriceTier(tier_id=tier['tier_id'],run_id=run.run_id,evidence_id=tier['evidence_id'],payload=json.dumps(tier,ensure_ascii=False)))
    supplier=[f for f in result['fields'] if f['field_name'] in SUPPLIER_FIELDS]
    previous=s.get(SupplierSnapshot,run.run_id)
    if previous:previous.payload=json.dumps(supplier,ensure_ascii=False)
    else:s.add(SupplierSnapshot(run_id=run.run_id,payload=json.dumps(supplier,ensure_ascii=False)))
    for p in result['review_candidates']:
        if not s.get(PainPoint,p['pain_id']):s.add(PainPoint(run_id=run.run_id,**p))
    for kind in ('主图','功能图','参数图','场景图'):
        if not s.get(AssetRequirement,(run.product_id,kind)):
            s.add(AssetRequirement(product_id=run.product_id,asset_type=kind))
    return result

def _backfill_collected_skus(s, run, mapped_skus):
    """把新版映射识别出的 SKU 补写到结构化 SKU 表。

    早期扩展没有把 SKU 选项写入 ``snapshot.skus``，但新版映射器可以从
    SKU 文本区域恢复“名称、单价、库存”。回填只新增确定性 ID 不存在的
    记录，因此重复启动不会制造重复 SKU，也不会删除人工维护的旧记录。
    """
    existing=set()
    for row in s.scalars(select(CollectedSku).where(CollectedSku.run_id==run.run_id)):
        try:spec=json.loads(row.spec_json)
        except (TypeError,ValueError):spec={}
        existing.add((str(spec.get('text') or '').strip(),row.price_cny,row.stock))
    for index, sku in enumerate(mapped_skus if isinstance(mapped_skus,list) else []):
        if not isinstance(sku,dict) or not str(sku.get('text') or '').strip():continue
        semantic_key=(str(sku['text']).strip(),sku.get('price_cny'),sku.get('stock'))
        if semantic_key in existing:continue
        payload={
            'text':str(sku['text']).strip()[:1000],
            'evidence_id':sku.get('evidence_id'),
            'scope':sku.get('scope'),
            # SKU 与图片的关系必须来自页面选择状态。这里原样保留扩展证据，
            # 不根据图片内容猜测颜色、材质或脚架类型。
            'dimension':sku.get('dimension'),
            'image_relation_type':sku.get('image_relation_type'),
            'displayed_image_changed':sku.get('displayed_image_changed'),
            'source_locator':sku.get('source_locator'),
        }
        identity=json.dumps(payload,sort_keys=True,ensure_ascii=False)
        sku_id='SKU-'+hashlib.sha256(f'{run.run_id}|mapped|{index}|{identity}'.encode()).hexdigest()[:20]
        if s.get(CollectedSku,sku_id):continue
        s.add(CollectedSku(
            sku_id=sku_id,run_id=run.run_id,product_id=run.product_id,
            spec_json=json.dumps(payload,ensure_ascii=False),
            price_cny=sku.get('price_cny'),stock=sku.get('stock'),image_url=sku.get('image_url') or None,
            verification_status='待复核'))
        existing.add(semantic_key)

def backfill_mappings(root):
    """加表之后从现有快照幂等回填，不改商品、评分或历史内容。"""
    with session_scope(root) as s:
        runs=list(s.scalars(select(CollectionRun).order_by(CollectionRun.created_at)))
        for run in runs:
            if run.product_id:
                result=persist_mapping(s,run,json.loads(run.raw_snapshot_json))
                _backfill_collected_skus(s,run,result.get('skus',[]))
                # 同步运行时规范结果，原始快照和 CollectedFact.raw_value 保留
                # 不变，以便审计旧解析为何得到错误值。
                try:normalized=json.loads(run.normalized_json or '{}')
                except (TypeError,ValueError):normalized={}
                normalized['facts']={k:(result['resolved'].get(k) or '') for k in CATALOG}
                normalized['skus']=result.get('skus',[])
                normalized['mapping_version']=result['mapping_version']
                normalized['conflicts']=result.get('conflicts',[])
                run.normalized_json=json.dumps(normalized,ensure_ascii=False)
                for fact in s.scalars(select(CollectedFact).where(CollectedFact.run_id==run.run_id)):
                    value=result['resolved'].get(fact.field_name)
                    if value is not None:fact.normalized_value=str(value)

def read_mapping(s,run_id):
    record=s.get(OfferSnapshot,run_id)
    if not record:return None
    data=json.loads(record.payload)
    data['review_fields']=review_fields(data)
    anomalies={x['review_id']:x for x in detect_review_anomalies(data['review_fields'])}
    for field in data['review_fields']:
        if field.get('review_id') in anomalies:field['anomaly_reason']=anomalies[field['review_id']]['reason']
    data['anomalies']=list(anomalies.values())
    run=s.get(CollectionRun,run_id)
    data['content_claims']=[]
    for field in data['review_fields']:
        if not field['included'] or field['field_name'] not in CONTENT_FIELDS:continue
        cid=uid('CL-',run_id,field['review_id'])
        published=bool(run.status=='已人工复核' and s.get(ProductClaim,cid))
        data['content_claims'].append(dict(field_id=field['review_id'],field_name=field['field_name'],
            value=field['value'],evidence_id=field.get('evidence_id'),source_kind=field['source_kind'],
            usage_status='已人工复核' if published else '待人工复核',claim_id=cid if published else None))
    return data

def confirm_mapping(s,run):
    """人工确认后发布无冲突的内容字段；价格、店铺宣传及评论不进入事实文案池。"""
    data=read_mapping(s,run.run_id)
    if not data:return
    reviewed=[x for x in data['review_fields'] if x['included']]
    present={x['field_name'] for x in reviewed if str(x.get('value') or '').strip()}
    missing=COMMON_FIELDS-present
    if missing:
        names='、'.join(CATALOG[x][0] for x in sorted(missing))
        raise ValueError('商品通用属性未填写：'+names+'；请采集、AI辅助后复核或人工补录')
    for c in reviewed:
        if c['field_name'] not in CONTENT_FIELDS:continue
        cid=uid('CL-',run.run_id,c['review_id'])
        same=s.scalar(select(ProductClaim).where(ProductClaim.product_id==run.product_id,
            ProductClaim.field_name==c['field_name'],ProductClaim.value==c['value']))
        if not same and not s.get(ProductClaim,cid):
            s.add(ProductClaim(claim_id=cid,product_id=run.product_id,field_name=c['field_name'],value=c['value'],
                source_field=c.get('evidence_id') or (('ai-inference:' if c['source_kind']=='AI推测' else 'manual-review:')+run.run_id),
                derivation_rule=c['source_kind']+'；人工确认；'+VERSION,
                confidence=float(c.get('confidence',1.0)),
                verification_status='人工确认的AI推测' if c['source_kind']=='AI推测' else '已人工复核'))
    p=s.get(Product,run.product_id)
    chosen={x['field_name']:x['value'] for x in reviewed}
    if chosen.get('title'):p.title=chosen['title']
    if chosen.get('category'):p.category=chosen['category']
    p.verification_status='已人工复核'
    record=s.get(OfferSnapshot,run.run_id);payload=json.loads(record.payload)
    payload.setdefault('review_draft',{})['status']='已确认';record.payload=json.dumps(payload,ensure_ascii=False)
