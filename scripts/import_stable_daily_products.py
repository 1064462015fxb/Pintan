"""将登录后 1688 搜索页中可见的日用品导入稳定选品池。

稳定池与爆款表、爆款搜索表完全独立，不复用其中任何商品。
价格、展示成交量、供应商和商品 ID 来自当次搜索页；其他缺失数值
使用明确标注的保守代理值，不冒充 1688 平台事实。
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date
from pathlib import Path

from sqlalchemy import delete

from pintan.database import (
    BusinessRow, Product, ProductAsset, ProductClaim, StableScore, session_scope,
)

ROOT = Path(__file__).resolve().parents[1]
TODAY = date.today().isoformat()

# offer_id, 类目, 搜索词, 标题, 搜索页展示价, 展示成交下界, 供应商, 标题可见特征
ITEMS = [
    ("1041267987197", "日用清洁/垃圾袋", "垃圾袋家用加厚", "杰淏抽绳式加厚手提免撕垃圾袋", 0.04, 100000, "潍坊景灏塑料制品有限公司", "抽绳|手提|加厚|免撕"),
    ("1031020240561", "日用清洁/垃圾袋", "垃圾袋家用加厚", "艾草免撕加厚手提厨余抽绳垃圾袋", 0.08, 100000, "义乌市聚悦日用百货有限公司", "抽绳|手提|加厚|免撕|厨余"),
    ("1001009399770", "日用清洁/垃圾袋", "垃圾袋家用加厚", "免撕抽绳手提加厚大号垃圾袋", 1.80, 100000, "浦江屿欢日用品有限公司", "抽绳|手提|加厚|免撕|大号"),
    ("736208388272", "日用清洁/垃圾袋", "垃圾袋家用加厚", "车载办公室宿舍桌边自粘垃圾袋", 0.70, 100000, "金华市荆珏包装材料有限公司", "车载|办公|桌边|自粘"),
    ("687388844935", "日用清洁/抹布", "超细纤维抹布家用", "超细纤维餐具擦拭吸水百洁布", 2.00, 35000, "苏州亿倍智能清洁股份有限公司", "超细纤维|吸水|餐具|华夫格"),
    ("631480839842", "日用清洁/抹布", "超细纤维抹布家用", "超细纤维鱼鳞擦玻璃无痕抹布", 0.52, 28000, "常熟市洁永家居用品有限公司", "超细纤维|鱼鳞布|擦玻璃|无痕|加厚"),
    ("1033971771381", "日用清洁/抹布", "超细纤维抹布家用", "超细纤维多功能吸水厨房百洁布", 0.95, 3000, "河北华柔纺织品贸易有限公司", "超细纤维|吸水|厨房|多功能"),
    ("1045399391359", "日用清洁/抹布", "超细纤维抹布家用", "抽取式超细纤维干湿两用厨房抹布", 2.04, 400, "义乌市檬心日用品有限公司", "抽取式|超细纤维|干湿两用|厨房"),
    ("815381888706", "家居收纳/衣架", "无痕衣架家用", "双位无痕防滑防肩角家用衣架", 0.66, 100000, "义乌市昊妍贸易有限公司", "双位|无痕|防滑|防肩角"),
    ("955390500555", "家居收纳/衣架", "无痕衣架家用", "家用无痕防滑防肩角衣架", 0.40, 100000, "义乌市伊刃日用品有限公司", "无痕|防滑|防肩角|衣柜收纳"),
    ("917592587987", "家居收纳/衣架", "无痕衣架家用", "毛衣无痕防肩角防滑衣架", 6.21, 100000, "1688选品中心官方供应链", "毛衣|无痕|防肩角|防滑"),
    ("916136297999", "家居收纳/衣架", "无痕衣架家用", "极简凹槽节省空间防肩角无痕衣架", 0.90, 100000, "义乌市衣享家居用品有限公司", "无痕|防肩角|凹槽|节省空间"),
    ("989269199025", "家居收纳/桌面收纳", "桌面收纳盒家用", "化妆品杂物零食塑料桌面收纳盒", 0.17, 17000, "义乌市椿涧百货有限公司", "桌面|化妆品|杂物|零食|塑料"),
    ("1000165866257", "家居收纳/桌面收纳", "桌面收纳盒家用", "床头柜客厅茶几多功能纸巾收纳盒", 13.80, 8400, "台州市博蓝塑业有限公司", "纸巾盒|床头柜|客厅|茶几|多功能"),
    ("1062200679967", "家居收纳/桌面收纳", "桌面收纳盒家用", "日式厨房客厅抽屉零食杂物收纳盒", 0.08, 8400, "义乌市物源佬塑料制品有限公司", "日式|厨房|客厅|抽屉|零食|杂物"),
    ("848234816542", "家居收纳/桌面收纳", "桌面收纳盒家用", "客厅床头茶几遥控器多功能纸巾收纳盒", 12.80, 5500, "揭阳市嘉尔纳塑业有限公司", "纸巾盒|客厅|床头|茶几|遥控器|多功能"),
]


def main() -> None:
    with session_scope(ROOT) as session:
        # 用户已授权替换上一版稳定池；爆款与搜索业务行不动。
        session.execute(delete(BusinessRow).where(BusinessRow.kind == "stable"))
        session.execute(delete(StableScore))
        session.execute(delete(ProductClaim).where(ProductClaim.claim_id.like("CL-ST-%")))

        for offer_id, category, keyword, title, price, sales, supplier, tags in ITEMS:
            pid = f"1688-{offer_id}"
            stable_id = f"ST-DAY-{offer_id}"
            url = f"https://detail.1688.com/offer/{offer_id}.html"
            product = session.get(Product, pid)
            if not product:
                product = Product(product_id=pid, title=title, category=category,
                                  source_platform="1688", source_product_url=url,
                                  verification_status="搜索页已读取待详情复核")
                session.add(product)

            shipping = 3.0
            landed = price + shipping
            supplier_proxy = round(min(75.0, 45.0 + 6.0 * math.log10(max(1, sales))), 1)
            row = {
                "data_origin": "live_1688_search_daily_goods",
                "field_provenance": "标题/价格/展示成交/供应商/链接=登录后1688搜索页；运费/售价/供应商质量=演示推导",
                "source_platform": "1688", "verification_status": "待复核",
                "source_keyword": keyword,
                "source_query_url": f"https://s.1688.com/selloffer/offer_search.htm?keywords={keyword}",
                "product_url": url, "collected_at": TODAY, "category": category, "title": title,
                "wholesale_price_cny": f"{price:.2f}", "moq": "0", "sales_30d": str(sales),
                "repurchase_rate_pct": "0", "supplier_years": "0",
                "supplier_quality_proxy_pct": str(supplier_proxy), "ship_from": "",
                "material": "超细纤维" if "超细纤维" in title else ("塑料" if "塑料" in title else ""),
                "battery_capacity_mah": "", "power_w": "", "feature_tags": tags,
                "color_options": "", "estimated_shipping_cny": f"{shipping:.2f}",
                "stable_id": stable_id, "reference_retail_price_cny": f"{landed * 2:.2f}",
                "reference_price_provenance": "演示假设：(搜索页展示价+单件运费代理值3元)×2",
                "heat_score": "", "competition_score": "", "margin_score": "",
                "differentiation_score": "", "trust_score": "", "total_score": "",
                "stable_rank": "", "score_version": "", "scored_at": "",
                "supplier_name": supplier,
            }
            session.add(BusinessRow(kind="stable", row_id=stable_id,
                                    payload=json.dumps(row, ensure_ascii=False)))

            facts = (
                ("wholesale_price_cny", f"{price:.2f}", "1688搜索页展示价直接映射", .90),
                ("displayed_sales_floor", str(sales), "搜索页展示成交转数值下界，非近30天统计", .75),
                ("supplier_name", supplier, "1688搜索页供应商名称直接映射", .90),
                ("supplier_quality_proxy_pct", str(supplier_proxy), "展示成交下界对数映射并封顶75，非1688等级", .45),
                ("estimated_shipping_cny", "3.00", "无物流模板时的演示成本假设", .30),
            )
            for field, value, rule, confidence in facts:
                cid = "CL-ST-" + hashlib.sha256(f"{pid}|{field}|{value}".encode()).hexdigest()[:16]
                session.add(ProductClaim(claim_id=cid, product_id=pid, field_name=field,
                                         value=value, source_field="1688搜索页|演示推导",
                                         derivation_rule=rule, confidence=confidence,
                                         verification_status="待复核"))
            for asset_type in ("主图", "功能图", "参数图", "场景图"):
                aid = "AS-" + hashlib.sha256(f"{pid}|{asset_type}".encode()).hexdigest()[:16]
                if not session.get(ProductAsset, aid):
                    session.add(ProductAsset(asset_id=aid, product_id=pid,
                                             asset_type=asset_type, status="待详情采集"))

    print(f"imported stable daily products={len(ITEMS)}")


if __name__ == "__main__":
    main()
