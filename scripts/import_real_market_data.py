"""把人工打开并核验的淘宝爆款及1688搜索候选写入共享数据库。

本脚本只保存页面当次可见事实；搜索结果中的营销措辞不等同于已验证功效。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from pathlib import Path

from pintan.database import (
    BusinessRow,
    Product,
    ProductAsset,
    ProductClaim,
    SearchCandidate,
    TrendSignal,
    session_scope,
)

ROOT = Path(__file__).resolve().parents[1]
TODAY = date.today().isoformat()


TRENDS = [
    {
        "trend_id": "TR-REAL-TB-851716044280", "platform": "淘宝", "verification_status": "已读取待复核",
        "observed_date": TODAY, "title": "BKT人体工学护腰坐垫椅座一体靠托", "category": "家居/坐垫靠垫",
        "displayed_sales_range": "已售300+", "retail_price_cny": "195", "core_features": "坐靠一体|透气|多尺寸|腰部支撑",
        "target_audience": "久坐办公人群|学生|成人", "use_scenarios": "家居|办公|车载",
        "negative_review_pain_points": "支撑效果因人而异|尺寸适配风险", "pain_point_evidence_summary": "页面问答存在正反反馈；尺码按体重区间选择",
        "search_keyword": "人体工学护腰坐垫", "source_entry_url": "https://detail.tmall.com/item.htm?id=851716044280",
    },
    {
        "trend_id": "TR-REAL-TB-953032790362", "platform": "淘宝", "verification_status": "已读取待复核",
        "observed_date": TODAY, "title": "亚朵星球深睡枕Pro4侧睡记忆棉枕", "category": "家纺/枕芯",
        "displayed_sales_range": "", "retail_price_cny": "", "core_features": "记忆棉|侧睡|枕芯枕套组合",
        "target_audience": "成人|侧睡人群", "use_scenarios": "卧室睡眠",
        "negative_review_pain_points": "高度与软硬度适配风险|助眠功效需证据", "pain_point_evidence_summary": "标题可确认记忆棉和侧睡定位，具体功效待复核",
        "search_keyword": "记忆棉护颈枕", "source_entry_url": "https://detail.tmall.com/item.htm?id=953032790362",
    },
    {
        "trend_id": "TR-REAL-TB-669170574398", "platform": "淘宝", "verification_status": "已读取待复核",
        "observed_date": TODAY, "title": "超威羟哌酯户外便携防蚊喷雾", "category": "个护家清/驱蚊用品",
        "displayed_sales_range": "", "retail_price_cny": "", "core_features": "羟哌酯|喷雾|户外便携",
        "target_audience": "户外人群", "use_scenarios": "户外|露营|日常防蚊",
        "negative_review_pain_points": "成分浓度与适用人群需核验|功效时长需检测证据", "pain_point_evidence_summary": "标题只证明商品宣称，农药登记及适用范围须逐SKU核验",
        "search_keyword": "羟哌酯驱蚊喷雾", "source_entry_url": "https://detail.tmall.com/item.htm?id=669170574398",
    },
]


# offer_id、标题、页面展示价、页面展示销量、供应商。空 offer_id 表示搜索结果真实存在但详情ID未暴露。
CANDIDATES = {
    TRENDS[0]["trend_id"]: [
        ("896126910954", "人体工学护腰坐垫学生矫正坐姿椅腰靠", "5", "3.5万+件", "永康市锘衍工贸有限公司"),
        ("1054544462264", "人体工学护腰坐垫靠背腰部支撑", "9.3", "9700+件", "义乌市庆莹日用品有限公司"),
        ("915601536324", "人体工学护腰坐垫腰托座椅靠垫", "39", "1400+件", "湖南纵马科技发展有限公司"),
        ("1040718812000", "护腰坐垫靠背一体式坐垫", "8", "1.0万+件", "永康市筱扬工贸有限公司"),
        ("976526396822", "人体工学护腰坐垫加大款", "42", "1300+件", "东阳市可米乔供应链管理有限公司"),
        ("740074581184", "思菲其护腰坐垫靠背一体", "75", "4.9万+件", "东莞市思菲其健康产业有限公司"),
        ("964409242990", "人体学护腰坐垫腰部支撑靠垫", "6.5", "3.2万+件", "义乌市汪虎电子商务商行"),
        ("898326108482", "办公室护腰坐垫靠背一体", "18", "1.3万+件", "河北驰力医疗科技有限公司"),
        ("1050648788872", "记忆棉人体工学护腰坐垫", "5.48", "600+件", "义乌市恬顷电子商务商行"),
        ("", "昌峰人体工学椅护腰坐垫跨境款", "33.9", "400+件", "永康昌峰体育用品有限公司"),
    ],
    TRENDS[1]["trend_id"]: [
        ("1045253137836", "深睡三芯慢回弹记忆枕Pro3.0", "41", "500+件", "南通朵蔓青纺织品有限公司"),
        ("916688199308", "冰丝U型记忆棉护颈枕", "19.82", "5.1万+件", "兰溪市桔子家纺厂"),
        ("1060724313559", "深睡枕PRO记忆枕枕芯枕套组合", "50.49", "400+件", "南通豪与笑纺织品有限公司"),
        ("965676526970", "旅行U型记忆棉护颈枕", "5.98", "10万+件", "蠡县顺驰汽车装饰用品厂"),
        ("1031028342754", "慢回弹记忆棉深睡枕", "77.16", "1.3万+件", "南通梦麒安芯纺织品有限公司"),
        ("1053730548316", "酒店款慢回弹记忆护颈枕", "64.62", "2300+件", "南通枕月栖家纺有限公司"),
        ("1051075551941", "旅行便携U型记忆棉枕", "5.38", "5300+件", "东阳昱鑫新材料有限公司"),
        ("977469870211", "凉感零压牵引记忆棉枕", "23", "9900+件", "南通梦麒安芯纺织品有限公司"),
        ("1072642789825", "C型护颈按摩枕", "74", "400+件", "义乌市率宏电子商务商行"),
        ("545627697662", "酒店同款深睡记忆棉枕", "55.86", "500+件", "海门市轩云家用纺织品厂"),
    ],
    TRENDS[2]["trend_id"]: [
        ("1058270606942", "润本20%羟哌酯喷雾", "9", "500+件", "徐州市好享贸易有限公司"),
        ("1050227348992", "超威羟哌酯便携防蚊喷雾", "12.9", "10+件", "长丰立客赢商贸有限公司"),
        ("678243320730", "润本20%羟哌酯驱蚊液喷雾", "9.5", "8600+件", "江苏晨露婴儿用品有限公司"),
        ("908250918637", "闪客20%羟哌酯无香喷雾", "8.4", "700+件", "东阳市哲思日用品有限公司"),
        ("1044127688048", "润本15%羟哌酯便携倒喷瓶", "9.5", "10万+件", "1688小店进货官方供应链"),
        ("1024558129604", "超威羟哌酯橘子味喷雾", "14.9", "400+件", "贵阳市国平电子商务有限公司"),
        ("786736111281", "润本20%羟哌酯户外防蚊喷雾", "19.08", "100+件", "江苏晨露婴儿用品有限公司"),
        ("1045283959601", "户外20%羟哌酯驱蚊液喷雾", "19.1", "", "杭州悠悦贸易有限公司"),
        ("645451760244", "羟哌酯驱蚊液50毫升喷剂", "20", "", "北京米诺托电子商务有限公司"),
        ("1059593233849", "派卡瑞丁羟哌酯户外喷雾", "2.2", "10+件", "徐州博观电子商务有限公司"),
    ],
}


def detail_url(offer_id: str) -> str:
    return f"https://detail.1688.com/offer/{offer_id}.html" if offer_id else ""


def claim_id(pid: str, field: str, value: str) -> str:
    return "CL-" + hashlib.sha256(f"{pid}|{field}|{value}".encode()).hexdigest()[:16]


def main() -> None:
    with session_scope(ROOT) as session:
        for trend in TRENDS:
            payload = {"data_origin": "live_page_observation", "field_provenance": "登录后页面可见字段", **trend}
            session.add(TrendSignal(trend_id=trend["trend_id"], payload=json.dumps(payload, ensure_ascii=False)))
            session.add(BusinessRow(kind="trend", row_id=trend["trend_id"], payload=json.dumps(payload, ensure_ascii=False)))

            for index, (offer_id, title, price, sales, supplier) in enumerate(CANDIDATES[trend["trend_id"]], 1):
                pid = f"1688-{offer_id}" if offer_id else f"1688-SEARCH-{trend['trend_id']}-{index:02d}"
                search_id = f"SR-{trend['trend_id']}-{index:02d}"
                url = detail_url(offer_id)
                session.add(Product(product_id=pid, title=title, category=trend["category"], source_platform="1688", source_product_url=url or None, verification_status="搜索页已读取待详情复核"))
                facts = {"wholesale_price_cny": price, "displayed_sales": sales, "supplier_name": supplier}
                for field, value in facts.items():
                    if value:
                        session.add(ProductClaim(claim_id=claim_id(pid, field, value), product_id=pid, field_name=field, value=value, source_field="1688搜索结果页", derivation_rule="页面字段直接映射", confidence=0.9, verification_status="待详情复核"))
                for asset_type in ("主图", "功能图", "参数图", "场景图"):
                    aid = "AS-" + hashlib.sha256(f"{pid}|{asset_type}".encode()).hexdigest()[:16]
                    session.add(ProductAsset(asset_id=aid, product_id=pid, asset_type=asset_type, status="待详情采集"))
                row = {
                    "search_id": search_id, "trend_id": trend["trend_id"], "source_supply_id": pid,
                    "data_origin": "live_1688_search", "field_provenance": "登录后1688搜索页可见字段",
                    "source_platform": "1688", "verification_status": "搜索页已读取待详情复核",
                    "search_keyword": trend["search_keyword"],
                    "search_entry_url": f"https://s.1688.com/selloffer/offer_search.htm?keywords={trend['search_keyword']}",
                    "offer_url": url, "collected_at": TODAY, "category": trend["category"], "title": title,
                    "wholesale_price_cny": price, "estimated_shipping_cny": "", "moq": "",
                    "displayed_sales_range": sales,
                    "sales_30d": str(int(float(re.search(r"([0-9]+(?:\.[0-9]+)?)", sales).group(1)) * (10000 if "万" in sales else 1))) if re.search(r"([0-9]+(?:\.[0-9]+)?)", sales) else "0",
                    "sales_metric_note": "1688搜索页展示成交量的数值下界，非近30天成交",
                    "repurchase_rate_pct": "", "supplier_years": "", "supplier_quality_proxy_pct": "",
                    "material": "记忆棉（标题明示，待详情复核）" if "记忆棉" in title else "",
                    "battery_capacity_mah": "", "power_w": "",
                    # 只从标题字面提取；不使用模型补写未出现功能。
                    "feature_tags": "|".join(word for word in {
                        "家居/坐垫靠垫": ["人体工学", "护腰", "坐垫", "靠背", "一体", "记忆棉", "加大"],
                        "家纺/枕芯": ["记忆棉", "慢回弹", "护颈", "U型", "便携", "凉感", "零压", "酒店"],
                        "个护家清/驱蚊用品": ["羟哌酯", "喷雾", "便携", "户外", "无香", "倒喷", "15%", "20%"],
                    }[trend["category"]] if word.lower() in title.lower()),
                    "color_options": "", "supplier_name": supplier,
                }
                session.add(SearchCandidate(search_id=search_id, trend_id=trend["trend_id"], product_id=pid, payload=json.dumps(row, ensure_ascii=False)))
                session.add(BusinessRow(kind="search", row_id=search_id, payload=json.dumps(row, ensure_ascii=False)))

    print(f"imported trends={len(TRENDS)} candidates={sum(map(len, CANDIDATES.values()))}")


if __name__ == "__main__":
    main()
