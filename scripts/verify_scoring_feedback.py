"""单商品在线验收：备份SQLite，保存无密钥报告，调用一次评分模型及单图返工。"""
import json
import sqlite3
import time
from pathlib import Path
from dotenv import load_dotenv
from pintan.stable_estimation import estimate_stable
from pintan.database import session_scope, BusinessRow, GeneratedMedia
from pintan.image_workflow import generate_planned

root=Path(__file__).resolve().parents[1]
load_dotenv(root/'.env')
folder=root/'outputs'/('scoring-feedback-'+time.strftime('%Y%m%d-%H%M%S'))
folder.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(root/'data/pintan.db') as source,sqlite3.connect(folder/'before.db') as target:source.backup(target)
report={}
for name,action in [
    ('estimate',lambda:estimate_stable(root,'1688-1066393076121')),
    ('image',lambda:generate_planned(root,'PC-7444c64d77c946ad',['轮播图-1'],
        generation_mode='creative',audit_level='none',parent_media_id='MD-6ef6a3d5e3d34256b4536d6a9217e15c',
        user_feedback='保持商品结构、颜色和印花，缩小四周留白，让商品主体放大一些，背景保持干净白色。'))]:
    started=time.monotonic()
    try:
        value=action()
        report[name]={'status':'completed','seconds':round(time.monotonic()-started,2),
            'items':value.get('items',[]) if name=='image' else [],'cached':value.get('cached')}
    except Exception as exc:report[name]={'status':'failed','error_type':type(exc).__name__}
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({name:report[name]},ensure_ascii=False),flush=True)
with session_scope(root) as s:
    data=json.loads(s.get(BusinessRow,('stable','1688-1066393076121')).payload)
    report['scoring']={k:data.get(k) for k in ('score_status','total_score','score_coverage_pct','missing_score_fields',
        'scoring_evidence','reference_retail_price_cny','supplier_quality_proxy_pct','ai_estimate')}
(folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print('Report: '+str(folder/'report.json'),flush=True)
