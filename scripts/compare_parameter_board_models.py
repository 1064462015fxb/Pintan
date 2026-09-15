"""用统一多图证据和统一Prompt对比四个项目图片模型。

报告只保存模型、耗时、尺寸、文件哈希和错误类型，不保存密钥或完整响应。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

MODELS = ("qwen-image-2.0", "qwen-image-2.0-pro", "wan2.7-image", "wan2.7-image-pro")
SOURCES = (
    ROOT / "data/media/1688-1066393076121/generated/PC-7444c64d77c946ad/MD-a062f911e0274f47ac6cc95f3881ee32-attempt-1.png",
    ROOT / "outputs/parameter-model-comparison/reference-layout.png",
    ROOT / "outputs/parameter-model-comparison/reference-quality.png",
)

PROMPT = """Use case: infographic-diagram
Asset type: 淘宝商品详情页产品参数长图，竖版高完成度电商视觉。
输入图片角色：图片1是商品身份最高优先级证据；图片2只提供参数页结构参考；图片3只提供精致程度、色彩和排版质量参考。不得把图片2或图片3中的额外宣传语当成商品事实。

请直接生成一张完整成品图，不要输出解释。主体是米白色自立式车载垃圾袋，必须保持蓝色包边、蓝色笑脸与“Good luck”印花、袋口折边、柔软袋体材质和自立结构。采用浅冰蓝背景、白色圆角卡片、深蓝标题和少量黄色弧线点缀，整体清爽、专业、紧凑，达到图片3的商业完成度。

构图要求：
1. 顶部紧凑标题区，标题为“产品参数”。
2. 上半区用商品正面展开视角，水平双箭头标宽度25cm，垂直双箭头标高度18.5cm，箭头延长线必须连接商品边缘。
3. 下半区左侧用三分之四立体展开视角，再次标注宽25cm、高18.5cm、底部深度5.5cm；深度箭头必须指向袋体底部侧边。
4. 下半区右侧展示整齐的30只装包装状态；若无法依据输入保持真实，则使用简洁的数量信息卡，不得虚构复杂包装。
5. 无大面积空白，各模块对齐，商品主体清楚，尺寸线不穿过商品。

允许且必须逐字正确出现的文字只有：“产品参数”“展开尺寸”“25cm”“18.5cm”“5.5cm”“30只装”“Good luck”。除这些文字外不生成任何标语、功效、认证、价格、联系方式、平台Logo或说明。不要复制参考图中的“每个袋子都有标准”。

严格禁止：商品换色、印花拼写错误、改变袋口和底部结构、增加配件、错误数字、悬空箭头、箭头指错边、二维码、水印、促销标签、无痕/耐温/环保等无证据功效、人物和无关道具。"""

NEGATIVE = "错别字，乱码，错误尺寸，额外标语，商品变形，印花改变，悬空箭头，箭头穿过商品，虚构配件，水印，二维码，平台logo，低清晰度，大面积空白"


def data_url(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    with Image.open(path) as image:
        mime = Image.MIME.get(image.format, "image/png")
    return f"data:{mime};base64,{base64.b64encode(raw).decode()}", hashlib.sha256(raw).hexdigest()


def image_url(body: dict) -> str:
    choices = body.get("output", {}).get("choices", [])
    content = choices[0].get("message", {}).get("content", []) if choices else []
    for block in content:
        value = block.get("image") or block.get("image_url") if isinstance(block, dict) else None
        if isinstance(value, dict):
            value = value.get("url")
        if value:
            return str(value)
    raise ValueError("模型未返回图片地址")


def main() -> Path:
    key = os.getenv("QWEN_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未设置QWEN_API_KEY")
    missing = [str(path) for path in SOURCES if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"缺少对比输入：{missing}")
    endpoint = os.getenv(
        "QWEN_IMAGE_BASE_URL",
        "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
    ).strip()
    encoded = [data_url(path) for path in SOURCES]
    output = ROOT / "outputs/parameter-model-comparison"
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "prompt_version": "parameter-board-quality-v1",
        "prompt": PROMPT,
        "negative_prompt": NEGATIVE,
        "inputs": [{"file": str(path.relative_to(ROOT)), "sha256": digest}
                   for path, (_, digest) in zip(SOURCES, encoded)],
        "items": [],
    }
    with httpx.Client(timeout=300, follow_redirects=True) as client:
        for model in MODELS:
            started = time.monotonic()
            params = ({"n": 1, "size": "1024*1024", "prompt_extend": False,
                       "negative_prompt": NEGATIVE} if model.startswith("qwen-") else
                      {"n": 1, "size": "1K", "watermark": False})
            item = {"model": model, "parameters": params}
            try:
                content = [{"image": value} for value, _ in encoded]
                content.append({"text": PROMPT})
                response = client.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json={
                    "model": model,
                    "input": {"messages": [{"role": "user", "content": content}]},
                    "parameters": params,
                })
                response.raise_for_status()
                downloaded = client.get(image_url(response.json()))
                downloaded.raise_for_status()
                target = output / f"{model}.png"
                target.write_bytes(downloaded.content)
                with Image.open(target) as result:
                    item.update(status="success", file=str(target.relative_to(ROOT)), width=result.width,
                                height=result.height, sha256=hashlib.sha256(downloaded.content).hexdigest())
            except Exception as exc:
                item.update(status="failed", error_type=type(exc).__name__)
            item["elapsed_seconds"] = round(time.monotonic() - started, 2)
            report["items"].append(item)
            print(model, item["status"], item["elapsed_seconds"], flush=True)
    path = output / "comparison.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(main())
