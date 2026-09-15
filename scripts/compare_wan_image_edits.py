"""同一商品原图与语义约束下，对比万相2.7图片编辑模型。"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")
MODELS = ("wan2.7-image", "wan2.7-image-pro")
PROMPT = """Use case: ads-marketing。
以输入图为唯一商品外观参考，制作一张淘宝商品轮播场景图。将画面重构为干净、真实的汽车后排座椅背使用场景，垃圾袋自然垂直粘贴在座椅背面，接触关系明确，比例、透视、光线和阴影符合真实摄影。
严格保持商品不变：米白色方形袋身、原图袋口结构、褶皱、蓝色包边、正面蓝色 Good luck 笑脸印花及整体比例均与输入图一致。原图没有头枕挂带，严禁增加任何挂绳、绑带、挂钩或新的安装结构；不得更换款式、颜色、图案，不得增加第二个袋子。
移除原图中的所有中文广告文字、红色横线、手和咖啡杯。禁止商品变形、袋型改变、颜色或印花改变、额外文字、水印、促销标签、人物、悬浮、透视错误、插画感和低清晰度。输出1:1写实电商摄影图，商品清晰、主体突出、背景不过度装饰。"""


def extract_url(body: dict) -> str:
    choices = body.get("output", {}).get("choices", [])
    blocks = choices[0].get("message", {}).get("content", []) if choices else []
    for block in blocks:
        if isinstance(block, dict):
            value = block.get("image") or block.get("image_url")
            if isinstance(value, dict):
                value = value.get("url")
            if value:
                return str(value)
    raise ValueError("模型未返回图片地址")


def run(source: Path) -> Path:
    key = os.getenv("QWEN_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未设置 QWEN_API_KEY")
    endpoint = os.getenv("QWEN_IMAGE_BASE_URL", "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation").strip()
    raw = source.read_bytes()
    with Image.open(source) as opened:
        mime = str(opened.format or source.suffix.lstrip('.')).lower().replace("jpeg", "jpg")
    image_data = f"data:image/{mime};base64,{base64.b64encode(raw).decode()}"
    folder = ROOT / "outputs" / "wan-image-comparison"
    folder.mkdir(parents=True, exist_ok=True)
    report = {"input": source.name, "input_sha256": hashlib.sha256(raw).hexdigest(), "prompt": PROMPT,
              "parameters": {"size": "1K", "n": 1, "watermark": False}, "items": []}
    with httpx.Client(timeout=240, follow_redirects=True) as client:
        for model in MODELS:
            started = time.monotonic(); item = {"model": model}
            try:
                response = client.post(endpoint, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "input": {"messages": [{"role": "user", "content": [
                        {"image": image_data}, {"text": PROMPT}]}]},
                        "parameters": {"size": "1K", "n": 1, "watermark": False}})
                response.raise_for_status(); url = extract_url(response.json())
                image_response = client.get(url); image_response.raise_for_status()
                target = folder / f"{model}.png"; target.write_bytes(image_response.content)
                with Image.open(target) as result:
                    item.update(status="success", file=str(target.relative_to(ROOT)), width=result.width,
                                height=result.height, sha256=hashlib.sha256(image_response.content).hexdigest())
            except Exception as exc:
                item.update(status="failed", error_type=type(exc).__name__)
            item["elapsed_seconds"] = round(time.monotonic() - started, 2)
            report["items"].append(item); print(model, item["status"], item["elapsed_seconds"], flush=True)
    report_path = folder / "comparison.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


if __name__ == "__main__":
    default = Path(r"C:\Users\Administrator\AppData\Local\Temp\codex-clipboard-fb5d5cf4-d5f6-40e6-9ffb-9b20ad2ba7d7.png")
    print(run(Path(sys.argv[1]) if len(sys.argv) > 1 else default))
