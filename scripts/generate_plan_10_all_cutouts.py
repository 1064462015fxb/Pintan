"""按数据库最新的10张图片规划，用全量抠图证据逐张生成预览。

每次调用的输入固定包含：
- 一张最高优先级商品身份图；
- 四张覆盖全部20个抠图的证据拼图；
- 一张由当前规划素材制作的目标比例构图引导图。

最后一张构图引导图用于控制 Wan 2.7 编辑输出比例，不是最终成品。
"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from pintan.database import ImagePlan, ProductAsset, session_scope  # noqa: E402
from pintan.image_workflow import generate_candidate  # noqa: E402
from test_all_cutouts_generation import (  # noqa: E402
    PRODUCT_ID, cutout_all, deduplicate, make_evidence_sheets,
)

MODEL = "wan2.7-image-pro"
PLAN_ID = "IP-ce2d935522be451e827f7f282d551ebc"
OUTPUT = ROOT / "outputs/plan-10-all-cutouts"


def _white_identity(path: Path) -> Path:
    """Wan官方不保证透明通道输入，转换成白底以稳定商品边缘。"""
    output = OUTPUT / "identity-white.jpg"
    image = Image.open(path).convert("RGBA")
    canvas = Image.new("RGB", image.size, "white")
    canvas.paste(image, (0, 0), image)
    canvas.save(output, "JPEG", quality=95)
    return output


def _layout_guide(paths: list[Path], section: str, index: int) -> tuple[Path, str]:
    """把当前槽位的直接证据放到目标比例画布；最后输入图决定输出比例。"""
    if section == "轮播图":
        size = (1024, 1024)
        api_size = "1024*1024"
    else:
        size = (1024, 1536)
        api_size = "1024*1536"
    canvas = Image.new("RGB", size, (239, 246, 252))
    # 极轻的纵向明暗层次仅用于帮助模型理解画布边界，不形成固定模板。
    overlay = Image.new("RGBA", size, (255, 255, 255, 0))
    for y in range(size[1]):
        alpha = int(55 * (1 - y / max(1, size[1] - 1)))
        Image.Image.paste(overlay, (255, 255, 255, alpha), (0, y, size[0], y + 1))
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    max_w = int(size[0] * 0.78)
    max_h = int(size[1] * (0.66 if section == "详情页" else 0.78))
    for offset, path in enumerate(paths[:3]):
        subject = Image.open(path).convert("RGBA")
        subject.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
        # 多参考时略微错位陈列；当前规划均为单参考，但保留通用能力。
        x = (size[0] - subject.width) // 2 + offset * 20
        y = (size[1] - subject.height) // 2 + offset * 24
        shadow = Image.new("RGBA", size, (0, 0, 0, 0))
        alpha = subject.getchannel("A").filter(ImageFilter.GaussianBlur(16))
        shadow_layer = Image.new("RGBA", subject.size, (25, 50, 75, 55))
        shadow_layer.putalpha(alpha.point(lambda value: value * 45 // 255))
        shadow.alpha_composite(shadow_layer, (x + 12, y + 18))
        canvas = Image.alpha_composite(canvas.convert("RGBA"), shadow)
        canvas.alpha_composite(subject, (x, y))
    target = OUTPUT / f"guide-{section}-{index:02d}.jpg"
    canvas.convert("RGB").save(target, "JPEG", quality=92)
    return target, api_size


def _prompt(slot: dict, identity_id: str, unique_count: int) -> str:
    section = slot["section"]
    target = "1:1淘宝轮播图" if section == "轮播图" else "2:3淘宝详情页模块图"
    text_policy = (
        "搜索首图不添加任何外围文案。" if section == "轮播图" and slot["index"] == 1
        else "只保留完成当前用途必需的简短中文；不得复制证据拼图的编号、素材ID或原图营销角标。"
    )
    return f"""用途类型：ads-marketing / product-mockup
输出位置：{section}第{slot['index']}张，{target}
本张目的：{slot['purpose']}

输入角色：图片1是最高优先级商品身份图；图片2至图片5是覆盖全部{unique_count}个抠图的全量证据拼图；最后一张是本槽位直接证据制作的构图和比例引导图。
商品身份锁定素材：{identity_id}。必须保持米白色袋体、蓝色包边、蓝色笑脸与Good luck印花、袋口折边及袋体比例。证据冲突时以图片1为准；不得混合CAR TRASH BAG等其他印花，不得增加不存在的配件。

规划指令：{slot['prompt']}
视觉标准：淘宝高完成度商业图片，真实材质与褶皱，边缘干净，主体清楚，信息层级明确，避免廉价模板感。{text_policy}
必须删除：证据编号、素材ID、平台Logo、价格、二维码、水印、联系方式、无证据认证与无证据功效；不要把手、杯子、垃圾或原图外围文案变成商品结构。
只输出单张完整成图，不输出解释、拼图边框或设计过程。"""


def _generate(job: dict, common_inputs: list[Path]) -> dict:
    started = time.monotonic()
    result = {key: job[key] for key in ("section", "index", "purpose", "source_asset_ids")}
    try:
        data = generate_candidate(job["prompt"], [*common_inputs, job["guide"]], MODEL,
                                  size=job["api_size"])
        target = OUTPUT / job["filename"]
        target.write_bytes(data)
        with Image.open(io.BytesIO(data)) as image:
            dimensions = [image.width, image.height]
        result.update(status="success", file=str(target.relative_to(ROOT)).replace("\\", "/"),
                      sha256=hashlib.sha256(data).hexdigest(), dimensions=dimensions)
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:500])
    result["elapsed_seconds"] = round(time.monotonic() - started, 2)
    print(result["section"], result["index"], result["status"],
          result["elapsed_seconds"], flush=True)
    return result


def main() -> Path:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    successes, cutout_failures = cutout_all()
    unique, duplicates = deduplicate(successes)
    if not unique:
        raise RuntimeError("没有可用的全量抠图证据")
    sheets, mapping = make_evidence_sheets(unique)
    identity = unique[0]
    identity_path = _white_identity(identity["path"])

    with session_scope(ROOT) as session:
        plan_row = session.get(ImagePlan, PLAN_ID)
        if not plan_row:
            raise RuntimeError("找不到指定的10图规划")
        slots = json.loads(plan_row.payload).get("slots", [])
        if len(slots) != 10:
            raise RuntimeError(f"规划不是10张：实际{len(slots)}张")
        assets = {asset.asset_id: asset for asset in session.query(ProductAsset).filter(
            ProductAsset.product_id == PRODUCT_ID).all()}

    cutout_by_id = {item["asset_id"]: item["path"] for item in successes}
    jobs = []
    for slot in slots:
        direct = [cutout_by_id[asset_id] for asset_id in slot["source_asset_ids"]
                  if asset_id in cutout_by_id]
        if not direct:
            raise RuntimeError(f"{slot['section']}-{slot['index']}没有成功抠图的直接证据")
        guide, api_size = _layout_guide(direct, slot["section"], slot["index"])
        filename = ("carousel" if slot["section"] == "轮播图" else "detail") + f"-{slot['index']:02d}.png"
        jobs.append({**slot, "guide": guide, "api_size": api_size, "filename": filename,
                     "prompt": _prompt(slot, identity["asset_id"], len(unique))})

    # 两路并发兼顾速度与接口稳定性；单图失败不会中止其他9张。
    results = []
    common_inputs = [identity_path, *sheets]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_generate, job, common_inputs) for job in jobs]
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: (0 if item["section"] == "轮播图" else 1, item["index"]))
    report = {
        "product_id": PRODUCT_ID, "plan_id": PLAN_ID, "model": MODEL,
        "cutout_success_count": len(successes), "cutout_failure_count": len(cutout_failures),
        "unique_cutout_count": len(unique), "duplicates": duplicates,
        "identity_asset_id": identity["asset_id"], "evidence_mapping": mapping,
        "input_strategy": "identity + 4 all-cutout evidence sheets + per-slot aspect guide",
        "items": results, "success_count": sum(x["status"] == "success" for x in results),
        "failure_count": sum(x["status"] != "success" for x in results),
        "total_elapsed_seconds": round(time.monotonic() - started, 2),
    }
    report_path = OUTPUT / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


if __name__ == "__main__":
    print(main())
