"""汇总真实测量，不将失败样本或缓存响应包装成冷启动提速。"""
import json
import statistics
import sys
from pathlib import Path

root = Path(sys.argv[1])
before = json.loads((root / "before/measurements.json").read_text(encoding="utf-8"))
after = json.loads((root / "after/measurements.json").read_text(encoding="utf-8"))

def summary(rows):
    good = [r for r in rows if r["status"] == "success"]
    data = {"attempts": len(rows), "successes": len(good), "success_rate": len(good)/len(rows)}
    for field in ("elapsed_seconds", "model_calls", "input_tokens", "output_tokens", "corrections"):
        values = [r[field] for r in good if r.get(field) is not None]
        data["median_" + field] = statistics.median(values) if values else None
    return data

b, a = summary(before), summary(after)
reduction = 1 - a["median_elapsed_seconds"] / b["median_elapsed_seconds"] if a["successes"] and b["successes"] else None
report = {"trend_id": "TR-DY-001", "model": "qwen3.7-plus", "before": b, "after": a,
    "latency_reduction_pct": round(reduction*100, 2) if reduction is not None else None,
    "target_met": bool(reduction is not None and reduction >= .30 and b["successes"] == a["successes"] == 3),
    "samples": {"before": before, "after": after},
    "limitations": "同一演示样本各三次、同环境同模型的顺序测试；结果不代表其他商品或服务负载下的固定耗时。"}
(root / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps({k:v for k,v in report.items() if k not in ('samples','limitations')}, ensure_ascii=False))
