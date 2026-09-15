"""对当前真实商品运行图文测试。仅输出阶段和ID，不打印请求/密钥。"""
import sys,json,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from pintan.content_flow import diagnose_angle,save_choice,generate_platform
from pintan.media_pipeline import generate_images
from pintan.data_store import atomic_write_json

if __name__=='__main__':
    started=time.monotonic();pid='1688-1066393076121'
    record={'product_id':pid,'scope':'用户授权本次测试，不自动上架'}
    path=ROOT/'outputs/current_listing_smoke.json'
    try:
        d=diagnose_angle(ROOT,pid);record['diagnosis_id']=d['diagnosis_id'];atomic_write_json(path,record);print('diagnosis',d['diagnosis_id'],flush=True)
        choice=save_choice(ROOT,d['diagnosis_id'],[d['recommendation']['primary']['type']])
        result=generate_platform(ROOT,d['diagnosis_id'],choice['choice_id'],'淘宝')
        record['content_id']=result['content_id'];atomic_write_json(path,record);print('content',result['content_id'],flush=True)
        images=generate_images(ROOT,result['content_id']);record['images']=images
        record['status']='success' if all(x['status']=='success' for x in images['items']) else 'partial'
    except Exception as exc:
        record['status']='failed';record['error_type']=type(exc).__name__
        # 仅本地控制台输出结构错误；HTTP错误不打印完整响应。
        if isinstance(exc,ValueError):print(str(exc)[:350],flush=True)
    record['elapsed_seconds']=round(time.monotonic()-started,2);atomic_write_json(path,record)
    print(json.dumps(record,ensure_ascii=True),flush=True)
