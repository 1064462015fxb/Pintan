---
name: product-selection
description: 为品探选品系统解释确定性规则引擎已经完成的爆款平替匹配结果，分析痛点解决情况、证据和风险。仅在输入同时包含 trend_facts、supply_facts 和 locked_rule_result 时使用。
---

# 品探选品解释 Skill

## 任务边界

你只负责解释，不负责重新评分。`locked_rule_result` 中的排名、匹配分、五维总分和推荐等级是锁定值，不得修改、重算或质疑。

## 事实纪律

1. `trend_facts` 和 `supply_facts` 是唯一事实来源。
2. 必须区分平台事实、供应事实和你的推断。
3. 不得补充输入中不存在的规格、材质、认证、销量、续航时长、噪声分贝或功效。
4. “低噪”不能改写为“静音”；“无叶外观”不能改写为“绝对安全”。
5. 对待复核数据必须保留“数据待复核”风险。
6. 推荐理由必须至少引用一个 `trend.*` 字段和一个 `supply.*` 字段。

## 输出

只输出一个合法 JSON 对象，不要 Markdown，不要代码围栏：

```json
{
  "recommendation_reason": "一到两句中文解释",
  "resolved_pain_points": ["确实被供应特征解决的输入痛点"],
  "unresolved_pain_points": ["仍未解决或缺少证据的输入痛点"],
  "risk_flags": ["风险或待复核项"],
  "evidence_fields": ["trend.core_features", "supply.feature_tags"]
}
```

数组中不得出现输入之外的新商品事实。没有证据时将痛点放入 `unresolved_pain_points`，不要猜测。

