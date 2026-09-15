"""独立样本、真实调用的前后基准；不复制密钥、不写回正在展示的业务表。"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--project", type=Path, required=True)
parser.add_argument("--code-root", type=Path, required=True)
parser.add_argument("--fixtures", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--runs", type=int, default=3)
args = parser.parse_args()
sys.path.insert(0, str(args.code_root / "src"))
from dotenv import load_dotenv
from langchain_core.callbacks import BaseCallbackHandler
from pintan import agents
from pintan.data_store import atomic_write_json

load_dotenv(args.project / ".env", override=False)
original_factory = agents._make_agent
args.output.mkdir(parents=True, exist_ok=True)


class Meter(BaseCallbackHandler):
    def __init__(self):
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.usage_observed = False
        self.invocations = 0

    def on_chat_model_start(self, *a, **kw):
        self.calls += 1

    def on_llm_end(self, response, **kw):
        for group in response.generations:
            for generation in group:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if usage:
                    self.usage_observed = True
                    self.input_tokens += usage.get("input_tokens", 0)
                    self.output_tokens += usage.get("output_tokens", 0)


results = []
for number in range(1, args.runs + 1):
    root = args.output / f"sample_{number}"
    if root.exists():
        raise FileExistsError(f"拒绝覆盖已有样本: {root}")
    root.mkdir()
    shutil.copytree(args.code_root / "skills", root / "skills")
    for filename in ("pintan_market_trending_products.csv", "pintan_trend_search_results.csv"):
        shutil.copy2(args.fixtures / filename, root / filename)
    meter = Meter()

    def factory(*a, **kw):
        agent, model = original_factory(*a, **kw)
        configured = agent.with_config(callbacks=[meter])

        class Counted:
            def invoke(self, *a, **kw):
                meter.invocations += 1
                return configured.invoke(*a, **kw)

        return Counted(), model

    agents._make_agent = factory
    started = time.monotonic()
    item = {"sample": number, "status": "failed"}
    print(f"sample {number} started", flush=True)
    try:
        result = agents.run_trending_agent(root, "TR-DY-001")
        item.update(status=result["status"], top3=[x["search_id"] for x in result["top3"]], model=result["model"])
    except Exception as error:
        item["error_type"] = type(error).__name__
    item.update(elapsed_seconds=round(time.monotonic() - started, 3), model_calls=meter.calls,
        input_tokens=meter.input_tokens if meter.usage_observed else None,
        output_tokens=meter.output_tokens if meter.usage_observed else None,
        corrections=max(0, meter.invocations-1))
    results.append(item)
    atomic_write_json(args.output / "measurements.json", results)
    print(json.dumps(item), flush=True)
