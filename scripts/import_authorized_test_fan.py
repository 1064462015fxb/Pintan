"""把历史1688风扇原图登记为可追溯测试素材；仅用于本地冒烟测试。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pintan.database import (AngleDiagnosis, ContentChoice, ContentMaster, ContentVersion,
    PlatformContent, PlatformContentVersion, Product, ProductClaim, session_scope, utcnow)
from pintan.media_pipeline import _cutout, authorize_asset, upload_asset

ROOT=Path(__file__).resolve().parents[1]
PRODUCT_ID="1688-898295719642"
OFFER_URL="https://detail.1688.com/offer/898295719642.html"


def main():
    with session_scope(ROOT) as s:
        if not s.get(Product,PRODUCT_ID):
            s.add(Product(product_id=PRODUCT_ID,title="五档风速随身夹式风扇（测试样本）",category="便携风扇",
                          source_platform="1688",source_product_url=OFFER_URL,verification_status="待复核"))
        for field,value in {"title":"五档风速随身夹式风扇","feature_tags":"五档风速调节",
                            "usage_scenario_candidate":"桌面、床头等夹持使用场景"}.items():
            claim_id="CL-"+hashlib.sha256(f"{PRODUCT_ID}|{field}|{value}".encode()).hexdigest()[:16]
            if not s.get(ProductClaim,claim_id):
                s.add(ProductClaim(claim_id=claim_id,product_id=PRODUCT_ID,field_name=field,value=value,
                    source_field="1688历史原图可见文字" if field!="usage_scenario_candidate" else "图片场景推导",
                    derivation_rule="原图文字转录，待复核" if field!="usage_scenario_candidate" else "根据图片中夹持形态推导",
                    confidence=.8 if field!="usage_scenario_candidate" else .5,verification_status="待复核"))

    imported=[]
    for path in sorted((ROOT/"data/1688_assets/898295719642").glob("*.jpg")):
        result=upload_asset(ROOT,PRODUCT_ID,path.read_bytes(),"image/jpeg")
        # 重跑脚本时也刷新抠图，使新版主体裁切算法作用于既有哈希记录。
        with session_scope(ROOT) as s:
            asset=s.get(__import__("pintan.database",fromlist=["ProductAsset"]).ProductAsset,result["asset_id"])
            cutout_path=ROOT/asset.cutout_path;cutout_path.write_bytes(_cutout(path.read_bytes()))
        authorize_asset(ROOT,result["asset_id"],True,"我已确认拥有该图片的使用权")
        with session_scope(ROOT) as s:
            asset=s.get(__import__("pintan.database",fromlist=["ProductAsset"]).ProductAsset,result["asset_id"])
            asset.source_url=OFFER_URL+f"#historical-{path.name}";asset.asset_type="1688历史原图";asset.status="测试授权已确认"
        imported.append(result["asset_id"])

    diagnosis_id="AD-TEST-898295719642";choice_id="CC-TEST-898295719642";master_id="CM-TEST-898295719642";content_id="PC-TEST-898295719642"
    with session_scope(ROOT) as s:
        if not s.get(AngleDiagnosis,diagnosis_id):s.add(AngleDiagnosis(diagnosis_id=diagnosis_id,product_id=PRODUCT_ID,
            cache_key="authorized-test-fan-v1",result_json=json.dumps({"test":True}),model="人工测试夹具",prompt_version="test-v1",created_at=utcnow()))
        if not s.get(ContentChoice,choice_id):s.add(ContentChoice(choice_id=choice_id,diagnosis_id=diagnosis_id,
            selected_json='["使用场景","差异化功能"]',custom_angle="",created_at=utcnow()))
        if not s.get(ContentMaster,master_id):
            s.add(ContentMaster(master_id=master_id,product_id=PRODUCT_ID,cache_key="authorized-test-fan-v1",status="测试草稿",current_version=1,created_at=utcnow()))
            s.flush();s.add(ContentVersion(master_id=master_id,version=1,content_json=json.dumps({"test":True}),status="测试草稿",model="人工测试夹具",prompt_version="test-v1",created_at=utcnow()))
        if not s.get(PlatformContent,content_id):
            s.add(PlatformContent(content_id=content_id,master_id=master_id,choice_id=choice_id,platform="淘宝",market="中国",
                cache_key="authorized-test-fan-taobao-v1",current_version=1,risk_level="中",created_at=utcnow()))
            s.flush();s.add(PlatformContentVersion(content_id=content_id,version=1,visible_json=json.dumps({
                "商品标题":"五档风速随身夹式风扇（参数待复核）","短标题":"随身夹式风扇",
                "核心卖点":["五档风速调节","夹式放置，适合桌面场景"],
                "视频钩子":"桌面空间有限，也能灵活放置","口播":["夹式结构方便桌面放置","五档风速可按需调节","实际参数请以商品页面为准"]},ensure_ascii=False),
                issues_json='[{"rule":"事实复核","text":"档位与使用场景需人工核验"}]',changes_json='[]',status="测试草稿",model="人工测试夹具",created_at=utcnow()))
    print(json.dumps({"product_id":PRODUCT_ID,"content_id":content_id,"authorized_assets":imported},ensure_ascii=False))


if __name__=="__main__":main()
