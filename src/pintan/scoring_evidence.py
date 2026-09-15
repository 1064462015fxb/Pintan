"""评分代理保留原文与统计口径，不把展示销量改写为近30天销量。"""
import re

def extract_signals(text):
    result={}
    match=re.search(r'已售\s*(\d+(?:\.\d+)?)\s*(万)?\s*(\+)?\s*(件|包|个|笔)',text)
    if match:
        result['sales_proxy']={'value':float(match[1])*(10000 if match[2] else 1),
            'raw':match[0],'period':'未注明','unit':match[4],'lower_bound':bool(match[3]),'source':'页面销量展示'}
    match=re.search(r'店铺回头率\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%',text)
    if match and float(match[1])<=100:
        result['repurchase_proxy']={'value':float(match[1]),'raw':match[0],'source':'店铺回头率代理'}
    # 不接受“满额包邮”“不包邮”或退货运费作为无条件采购运费。
    if '包邮' in text and not re.search(r'不包邮|满\s*\d+[^\n。；]{0,20}包邮|偏远[^\n。；]{0,12}包邮|包邮[^\n。；]{0,12}除外',text):
        destination=re.search(r'送至\s*([^\n，。]{2,20})',text)
        result['shipping_proxy']={'value':0,'raw':'包邮','source':'按页面当前配送条件',
            'destination':destination[1] if destination else None}
    return result
