"""真实原图OCR/视觉分类验收；快照仅记录状态，不保存密钥或模型请求。"""
import json,sqlite3,sys,time
from pathlib import Path
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from pintan.database import Base,engine
from pintan.image_workflow import classify_product,analyses,plan_images,generate_planned

if __name__=='__main__':
    backup=ROOT/'data/backups'/('pintan-before-image-workflow-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.db')
    backup.parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(ROOT/'data/pintan.db') as source,sqlite3.connect(backup) as dest:source.backup(dest)
    Base.metadata.create_all(engine(ROOT))
    pid='1688-1066393076121';start=time.monotonic()
    # 三张已知的外观/袋口/安装图；实际采集按钮不限制张数。
    result=classify_product(ROOT,pid,force=False,asset_ids=None if '--all' in sys.argv else {'AS-418c091dd66d269b','AS-4d131a0306ec2bd6','AS-02b39f5eea2a01ab'})
    result['elapsed_seconds']=round(time.monotonic()-start,2)
    result['analyses']=analyses(ROOT,pid)
    if '--generate' in sys.argv:
        # 仅生成一个规划位置，控制在线验收费用；普通页面生成不限一张。
        with sqlite3.connect(ROOT/'data/pintan.db') as db:
            row=db.execute('select content_id from platform_contents order by rowid desc limit 1').fetchone()
        if row:
            try:
                plan=plan_images(ROOT,row[0]);result['plan']=plan
                print('plan_created',len(plan['slots']),flush=True)
                from pintan.media_pipeline import authorize_asset
                for aid in plan['slots'][0]['source_asset_ids']:
                    authorize_asset(ROOT,aid,True,'用户已授权当前商品原图用于本次生成测试，不代表平台或权利人证明')
                slot=plan['slots'][0]
                result['generated']=generate_planned(ROOT,row[0],[f"{slot['section']}-{slot['index']}"])
            except Exception as exc:
                result['generation_error_type']=type(exc).__name__
                if isinstance(exc,ValueError):result['generation_error']=str(exc)[:200]
                print('generation_error',type(exc).__name__,flush=True)
    (ROOT/'outputs/image-workflow-smoke.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'items':result['items'],'generated':result.get('generated'),
        'generation_error':result.get('generation_error'),'elapsed_seconds':result['elapsed_seconds']},ensure_ascii=True),flush=True)
