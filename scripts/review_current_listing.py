"""本次测试复核：删除模型擅自增加的性能承诺，保留历史版本。"""
import sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from pintan.content_flow import _call,get_platform_content,edit_platform,scan
from pintan.database import product_bundle,session_scope,GeneratedMedia
from pintan.media_pipeline import generate_images
from pintan.data_store import atomic_write_json
from sqlalchemy import select
cid='PC-2b675cba51a842bd';pid='1688-1066393076121'
bundle=product_bundle(ROOT,pid);old=get_platform_content(ROOT,cid)
raw,_=_call(ROOT,{'mode':'review_listing','fields':old['fields'],'facts':bundle,
 'instruction':'删除防漏、不伤内饰、稳固、防倒、不倾倒、呕吐应急、PE性能推导等缺乏证据的表达，不要换同义词。保留车载垃圾收纳、粘贴悬挂、可自立和30只装。只返回fields对象，键保持原样，不新增参数，表述保守简洁。'})
fields=raw.get('fields',raw)
if set(fields)!=set(old['fields']):raise ValueError('文案改写结构不完整：'+','.join(fields.keys()))
if scan(fields,bundle,'淘宝'):raise ValueError('仍有无证据表述')
result=edit_platform(ROOT,cid,fields);print('reviewed version',result['version'],flush=True)
with session_scope(ROOT) as s:
    for image in s.scalars(select(GeneratedMedia).where(GeneratedMedia.content_id==cid)):
        meta=json.loads(image.metadata_json);meta['review_warning']='旧文案存在无证据性能表述，已被新版本替代';image.metadata_json=json.dumps(meta,ensure_ascii=False)
images=generate_images(ROOT,cid)
atomic_write_json(ROOT/'outputs/current_listing_smoke.json',{'content_id':cid,'product_id':pid,'content_version':result['version'],'images':images,'status':'success' if all(x['status']=='success' for x in images['items']) else 'partial','authorization_scope':'本次测试'})
print(json.dumps(images),flush=True)
