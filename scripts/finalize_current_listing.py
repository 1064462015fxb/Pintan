"""本次人工审校后的保守文案；复用成功AI背景重排图，不重复花费token。"""
import sys,json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from pintan.content_flow import get_platform_content,edit_platform
from pintan.media_pipeline import generate_images
from pintan.data_store import atomic_write_json
cid='PC-2b675cba51a842bd'
fields=get_platform_content(ROOT,cid)['fields']
fields['核心卖点']=['粘贴悬挂，车载垃圾收纳','可自立袋型','30只装，按所选规格供货']
fields['主图文案']=['车载垃圾收纳','粘贴悬挂 · 可自立','30只装']
fields['详情页模块']=[{'title':'商品介绍','content':'车载清洁收纳垃圾袋，用于整理日常杂物。'},
 {'title':'产品特点','content':'粘贴悬挂、可自立袋型。具体安装位置请结合实物与车辆情况确认。'},
 {'title':'规格选择','content':'本次展示30只装；另有60只、90只规格，按所选规格供货。'},
 {'title':'图片说明','content':'图片中的杯子及其他物品为场景展示，不在供货范围。'}]
fields['属性建议']=[x for x in fields['属性建议'] if x.get('name')!='材质']
r=edit_platform(ROOT,cid,fields)
images=generate_images(ROOT,cid,reuse_backgrounds=True)
atomic_write_json(ROOT/'outputs/current_listing_smoke.json',{'content_id':cid,'product_id':'1688-1066393076121','content_version':r['version'],'images':images,'status':'success' if all(x['status']=='success' for x in images['items']) else 'partial','scope':'本次测试，人工审校，未上架','backgrounds':'复用本次Qwen成功背景'})
print(json.dumps(images))
