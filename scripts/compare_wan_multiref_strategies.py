"""使用同一组商品证据图，生成三种万相 2.7 Pro 电商方案。

图片在请求中有明确角色：第一张用于锁定商品外观，后续图片只补充
袋口、站立形态与背胶安装方式，避免模型把场景图中的构图误当成产品结构。
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
MODEL = "wan2.7-image-pro"

# 三张图分别提供：商品正面外观、袋口/使用形态、背胶安装证据。
SOURCES = [
    Path(r"C:\Users\Administrator\AppData\Local\Temp\codex-clipboard-fb5d5cf4-d5f6-40e6-9ffb-9b20ad2ba7d7.png"),
    ROOT / "data/assets/1688-1066393076121/source/14589188a2cdb447ac8c391490976bf4676f429f64c19f1676f1b04f4199707b.jpg",
    ROOT / "data/assets/1688-1066393076121/source/8ad478abcde050e5d633057ed52211e1e2a3f938c212b8adc001fac144346b89.jpg",
]

COMMON = """Use case: ads-marketing。
输入图角色：图片1是商品正面外观的最高优先级证据；图片2补充袋口、容量和实际使用形态；图片3只用于证明背面无痕粘贴结构与安装动作。三张图描述的是同一款米白色方形车载垃圾袋。
严格保持：米白色袋身、方形比例、袋口折边、纸质/无纺布般褶皱、正面蓝色 Good luck 笑脸印花。固定方式只能是商品背面的粘贴结构，不得增加头枕挂带、绳索、挂钩、硬质支架、盖子或第二个袋子。
若图片之间冲突，以图片1的颜色、印花和比例为准，以图片3的固定方式为准。不得把参考图中的中文文案、尺寸标线、手、杯子或其他商品复制进成品。禁止新增品牌、促销文字、水印、虚构配件、人物、悬浮、透视错误和商品变形。商品必须与接触面形成可信接触和阴影。"""

SCHEMES = {
    "scheme-a-catalog": COMMON + """
方案A——保真商品主图：生成纯净暖白色摄影棚背景，商品单独居中、自然直立、袋口打开，略微三分之四视角，柔和棚拍光和自然投影。不要汽车背景，不要放入垃圾，不要任何文字。构图适合淘宝1:1商品轮播主图，商品占画面约75%。""",
    "scheme-b-installation": COMMON + """
方案B——真实安装场景：生成整洁现代汽车后排座椅背场景。商品通过背面粘贴结构自然垂直固定在座椅背面，粘贴位置和受力关系清晰可信，袋口打开但内部保持干净。画面为写实电商摄影，不出现手、杯子、文字或人物；商品占画面约55%，背景简洁。""",
    "scheme-c-premium": COMMON + """
方案C——高级生活方式场景：生成光线柔和、干净克制的高端汽车内饰场景，商品粘贴在副驾驶侧边平整区域，位置便于随手使用，袋口微开，旁边只有少量不抢主体的生活道具。暖灰与黑色内饰，真实自然光，商品占画面约50%，右上方保留干净留白供后期程序排版。不要直接生成任何文字。""",
}


def data_url(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    with Image.open(path) as image:
        mime = str(image.format or path.suffix.lstrip(".")).lower().replace("jpeg", "jpg")
    return f"data:image/{mime};base64,{base64.b64encode(raw).decode()}", hashlib.sha256(raw).hexdigest()


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


def main() -> Path:
    key = os.getenv("QWEN_API_KEY", "").strip()
    if not key:
        raise RuntimeError("未设置 QWEN_API_KEY")
    missing = [str(path) for path in SOURCES if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"缺少证据图：{missing}")

    endpoint = os.getenv(
        "QWEN_IMAGE_BASE_URL",
        "https://token-plan.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation",
    ).strip()
    encoded = [data_url(path) for path in SOURCES]
    output_dir = ROOT / "outputs" / "wan-multiref-strategies"
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "model": MODEL,
        "inputs": [
            {"file": str(path), "sha256": digest} for path, (_, digest) in zip(SOURCES, encoded)
        ],
        "parameters": {"size": "1K", "n": 1, "watermark": False},
        "schemes": [],
    }

    with httpx.Client(timeout=240, follow_redirects=True) as client:
        for name, prompt in SCHEMES.items():
            started = time.monotonic()
            record = {"name": name, "prompt": prompt}
            try:
                content = [{"image": value} for value, _ in encoded]
                content.append({"text": prompt})
                response = client.post(
                    endpoint,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={
                        "model": MODEL,
                        "input": {"messages": [{"role": "user", "content": content}]},
                        "parameters": {"size": "1K", "n": 1, "watermark": False},
                    },
                )
                response.raise_for_status()
                image_url = extract_url(response.json())
                downloaded = client.get(image_url)
                downloaded.raise_for_status()
                target = output_dir / f"{name}.png"
                target.write_bytes(downloaded.content)
                with Image.open(target) as result:
                    record.update(
                        status="success",
                        file=str(target.relative_to(ROOT)),
                        width=result.width,
                        height=result.height,
                        sha256=hashlib.sha256(downloaded.content).hexdigest(),
                    )
            except Exception as exc:
                record.update(status="failed", error_type=type(exc).__name__)
            record["elapsed_seconds"] = round(time.monotonic() - started, 2)
            report["schemes"].append(record)
            print(name, record["status"], record["elapsed_seconds"], flush=True)

    report_path = output_dir / "comparison.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


if __name__ == "__main__":
    print(main())
