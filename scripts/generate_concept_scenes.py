"""生成不含供应商图片输入的多场景痛点详情图预览。"""
from __future__ import annotations

import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from pintan.image_workflow import generate_candidate, render_concept_copy  # noqa: E402

OUTPUT = ROOT / "outputs/text-only-concept-scenes"
MODEL = "wan2.7-image-pro"

SCENES = [
    {
        "id": "kitchen",
        "title": "厨房零碎，随手容易堆积",
        "subtitle": "果皮、纸巾、小包装临时放，台面很快显得凌乱",
        "footer": "需要一个顺手的临时收纳位置",
        "scene": """现代小户型厨房操作台的一个连续全幅写实场景。浅木色台面与米白橱柜，备餐区有少量橘子皮、两张使用后的厨房纸和一个小食品包装，程度克制、卫生、真实；不出现腐烂食物、污水或夸张脏乱。自然晨光，高级家居商业摄影，画面有明确视觉焦点。严禁出现垃圾袋、收纳袋、垃圾桶、收纳盒、挂钩或任何解决方案产品。""",
    },
    {
        "id": "office",
        "title": "桌面零碎，越忙越容易乱",
        "subtitle": "便签、纸巾、小包装随手放，工作区域逐渐拥挤",
        "footer": "让临时收纳更靠近使用位置",
        "scene": """明亮现代办公室的一张连续全幅写实照片。原木办公桌上有笔记本电脑、少量便签、揉皱但干净的纸巾和一个小零食包装，呈现忙碌后轻微凌乱，整体仍专业整洁。柔和窗边日光，蓝灰与木色，高级办公生活方式摄影。严禁出现垃圾袋、收纳袋、垃圾桶、收纳盒、挂钩或任何解决方案产品。""",
    },
    {
        "id": "bedroom",
        "title": "生活零碎，随手放就容易散",
        "subtitle": "纸巾、标签、小包装缺少临时归置，影响空间整洁",
        "footer": "小空间也需要顺手的收纳方式",
        "scene": """温暖卧室梳妆台与床头区域的一张连续全幅写实照片。奶油色桌面上散落少量纸巾、拆下的商品标签和一个小型日用品包装，表现日常使用后的轻微零碎感，环境干净舒适。午后暖光，米白、浅木与低饱和蓝色，高级家居商业摄影。严禁出现垃圾袋、收纳袋、垃圾桶、收纳盒、挂钩或任何解决方案产品。""",
    },
]


def prompt(scene: dict) -> str:
    return (
        "Use case: photorealistic-natural。只生成竖版商业摄影无字底图，不是海报、信息图或排版稿。"
        "只允许一个连续场景，禁止四宫格、拼贴、分栏、卡片、步骤图、对比图和重复画面。"
        + scene["scene"]
        + "照片从顶部到底部连续铺满，不留标题区或人工留白。不要生成任何汉字、字母、数字、Logo、价格、二维码、水印、认证或功效承诺。"
    )


def run_one(scene: dict) -> dict:
    started = time.monotonic()
    result = {"id": scene["id"], "prompt": prompt(scene), "supplier_image_count": 0}
    try:
        raw = generate_candidate(result["prompt"], [], MODEL, size="768*1536")
        with Image.open(io.BytesIO(raw)) as image:
            image = image.convert("RGB")
            image.thumbnail((750, 1500), Image.Resampling.LANCZOS)
            canvas = Image.new("RGB", (750, 1500), "white")
            canvas.paste(image, ((750 - image.width) // 2, (1500 - image.height) // 2))
        items = [
            {"source_field": "模型创意.场景标题", "text": scene["title"]},
            {"source_field": "模型创意.场景解释", "text": scene["subtitle"]},
            {"source_field": "模型创意.承接语", "text": scene["footer"]},
        ]
        final = render_concept_copy(canvas, items)
        target = OUTPUT / f"{scene['id']}.png"
        final.save(target)
        result.update(status="success", file=str(target.relative_to(ROOT)).replace("\\", "/"),
                      dimensions=[final.width, final.height], copy=items)
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:500])
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    print(scene["id"], result["status"], result["elapsed_seconds"], flush=True)
    return result


def main() -> Path:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_one, scene) for scene in SCENES]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: [scene["id"] for scene in SCENES].index(item["id"]))
    report = {"model": MODEL, "generation_mode": "text-to-image-concept",
              "items": results, "total_elapsed_seconds": round(time.monotonic() - started, 2)}
    path = OUTPUT / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(main())
