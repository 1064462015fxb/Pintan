"""全量原图抠图后，作为同一商品的多视角证据生成两张测试图。"""
from __future__ import annotations

import hashlib
import io
import json
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

from pintan.asset_records import metadata, update_metadata  # noqa: E402
from pintan.database import AssetAnalysis, ProductAsset, session_scope, utcnow  # noqa: E402
from pintan.image_workflow import generate_candidate  # noqa: E402
from pintan.media_pipeline import _cutout  # noqa: E402

PRODUCT_ID = "1688-1066393076121"
MODEL = "wan2.7-image-pro"
OUTPUT = ROOT / "outputs/all-cutouts-generation"
CUTOUT_DIR = ROOT / "data/assets" / PRODUCT_ID / "cutouts"
TILES_PER_SHEET = 6
SHEET_SIZE = (1536, 1024)


def _json(value: str | None) -> dict:
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _analysis_payload(row: AssetAnalysis | None) -> dict:
    return _json(row.payload if row else None)


def _is_valid_asset(asset: ProductAsset, analysis: AssetAnalysis | None, meta: dict) -> bool:
    """只排除明确无效素材，不因网页区域仍是“待分类”而漏掉商品图。"""
    if not asset.local_path or not asset.sha256:
        return False
    if asset.status == "复核排除" or asset.status.startswith("已合并:"):
        return False
    if meta.get("review_included") is False:
        return False
    if min(asset.width or 0, asset.height or 0) <= 160:
        return False
    payload = _analysis_payload(analysis)
    return bool(analysis and analysis.status == "success" and payload.get("suitable") is True)


def _identity_priority(item: dict) -> tuple:
    """身份图优先产品正面，其次 SKU/主图，再按清晰度和面积排序。"""
    semantic_rank = {
        "产品正面图": 0, "SKU规格图": 1, "多角度图": 2, "使用场景图": 3,
        "功能卖点图": 4, "尺寸参数图": 5, "安装过程图": 6,
    }.get(item["semantic_type"], 9)
    region_rank = {"SKU图": 0, "主图": 1, "详情图": 2}.get(item["asset_type"], 3)
    return (semantic_rank, region_rank, -(item["width"] * item["height"]), item["asset_id"])


def cutout_all() -> tuple[list[dict], list[dict]]:
    """为全部合格原图抠图并记录路径；单张失败不阻断其他图片。"""
    CUTOUT_DIR.mkdir(parents=True, exist_ok=True)
    successes: list[dict] = []
    failures: list[dict] = []
    with session_scope(ROOT) as session:
        rows = (
            session.query(ProductAsset, AssetAnalysis)
            .outerjoin(AssetAnalysis, AssetAnalysis.asset_id == ProductAsset.asset_id)
            .filter(ProductAsset.product_id == PRODUCT_ID)
            .order_by(ProductAsset.asset_id)
            .all()
        )
        for asset, analysis in rows:
            meta = metadata(session, asset.asset_id)
            if not _is_valid_asset(asset, analysis, meta):
                continue
            source = (ROOT / asset.local_path).resolve()
            target = CUTOUT_DIR / f"{asset.asset_id}.png"
            payload = _analysis_payload(analysis)
            try:
                existing = (ROOT / asset.cutout_path).resolve() if asset.cutout_path else target
                if existing.is_file():
                    target = existing
                    data = target.read_bytes()
                    with Image.open(io.BytesIO(data)) as image:
                        if image.mode != "RGBA" or image.getchannel("A").getbbox() is None:
                            raise ValueError("已有抠图不是有效透明 PNG")
                    reused = True
                else:
                    if not source.is_file():
                        raise FileNotFoundError("原图文件不存在")
                    data = _cutout(source.read_bytes())
                    target.write_bytes(data)
                    reused = False
                asset.cutout_path = str(target.relative_to(ROOT)).replace("\\", "/")
                digest = hashlib.sha256(data).hexdigest()
                update_metadata(session, asset.asset_id, cutout_status="success",
                                cutout_engine="rembg-u2netp", cutout_sha256=digest,
                                cutout_updated_at=utcnow())
                successes.append({
                    "asset_id": asset.asset_id, "asset_type": asset.asset_type,
                    "semantic_type": str(payload.get("primary_type") or "未分类"),
                    "source": str(source.relative_to(ROOT)).replace("\\", "/"),
                    "cutout": str(target.relative_to(ROOT)).replace("\\", "/"),
                    "path": target, "sha256": digest, "width": int(asset.width or 0),
                    "height": int(asset.height or 0), "reused": reused,
                })
            except Exception as exc:
                update_metadata(session, asset.asset_id, cutout_status="failed",
                                cutout_error_type=type(exc).__name__, cutout_updated_at=utcnow())
                failures.append({"asset_id": asset.asset_id, "error_type": type(exc).__name__})
    return successes, failures


def deduplicate(items: list[dict]) -> tuple[list[dict], list[dict]]:
    """仅按抠图文件真实 SHA-256 去重，不用模型猜测。"""
    unique: list[dict] = []
    duplicates: list[dict] = []
    seen: dict[str, str] = {}
    for item in sorted(items, key=_identity_priority):
        canonical = seen.get(item["sha256"])
        if canonical:
            duplicates.append({"asset_id": item["asset_id"], "same_as": canonical})
        else:
            seen[item["sha256"]] = item["asset_id"]
            unique.append(item)
    return unique, duplicates


def _fit_rgba(path: Path, box: tuple[int, int]) -> Image.Image:
    image = Image.open(path).convert("RGBA")
    image.thumbnail(box, Image.Resampling.LANCZOS)
    return image


def make_evidence_sheets(items: list[dict]) -> tuple[list[Path], list[dict]]:
    """把全部去重抠图排成白底证据拼图，保存每格与素材ID的映射。"""
    OUTPUT.mkdir(parents=True, exist_ok=True)
    sheets: list[Path] = []
    mapping: list[dict] = []
    cell_w, cell_h = SHEET_SIZE[0] // 3, SHEET_SIZE[1] // 2
    font = ImageFont.load_default()
    for start in range(0, len(items), TILES_PER_SHEET):
        chunk = items[start:start + TILES_PER_SHEET]
        sheet_no = start // TILES_PER_SHEET + 1
        canvas = Image.new("RGB", SHEET_SIZE, "white")
        draw = ImageDraw.Draw(canvas)
        for offset, item in enumerate(chunk):
            row, col = divmod(offset, 3)
            x, y = col * cell_w, row * cell_h
            cutout = _fit_rgba(item["path"], (cell_w - 40, cell_h - 70))
            px = x + (cell_w - cutout.width) // 2
            py = y + 34 + (cell_h - 54 - cutout.height) // 2
            canvas.paste(cutout, (px, py), cutout)
            evidence_id = f"E{start + offset + 1:02d}"
            draw.text((x + 12, y + 10), f"{evidence_id} | {item['asset_id']}",
                      fill=(25, 45, 75), font=font)
            draw.rectangle((x, y, x + cell_w - 1, y + cell_h - 1),
                           outline=(210, 220, 232), width=2)
            mapping.append({"evidence_id": evidence_id, "sheet": sheet_no,
                            "asset_id": item["asset_id"],
                            "semantic_type": item["semantic_type"]})
        path = OUTPUT / f"evidence-sheet-{sheet_no:02d}.jpg"
        canvas.save(path, "JPEG", quality=91, optimize=True)
        sheets.append(path)
    return sheets, mapping


def prompts(identity_id: str, count: int, sheet_count: int) -> dict[str, str]:
    common = f"""你收到一张单独的最高优先级商品身份抠图，以及{sheet_count}张白底证据拼图；证据拼图完整包含该商品{count}个去重后的原图抠图视角。
单独身份图和证据 E01 用于锁定商品身份。其余证据用于理解正面、侧面、背面、展开结构、安装方式、材质、尺寸关系和真实使用场景，不得把原图中的手、杯子、垃圾、汽车内饰或营销文字当成商品组成部分。
最高优先级素材ID为 {identity_id}。当证据中印花、颜色、结构或附件冲突时，以最高优先级身份图为准，不得混合多个SKU，不得增加配件、改变材质、袋口、比例或商品自身印花。
证据图上的 E编号和素材ID仅用于索引，绝不能出现在最终画面。"""
    return {
        "carousel": common + """
直接输出一张可用于淘宝轮播的1:1高完成度商品主图。暖白到浅冰蓝摄影棚背景，商品以三分之四视角自然打开并自立，主体占画面约75%，柔和真实投影，边缘干净，材质和褶皱自然。画面只出现一个空袋，不出现手、杯子、垃圾、汽车、文字标题、角标、价格、平台Logo或水印。商品自身印花应忠于身份图，不要重写或新增文字。""",
        "detail": common + """
直接输出一张竖版3:4淘宝详情页功能展示图。采用浅冰蓝背景、白色圆角卡片、深蓝标题与少量黄色点缀，紧凑专业。上方展示商品正面，下方展示立体打开状态和一个干净的车内使用示意；三种视角必须保持为同一个SKU。允许新增的中文只有：“可立可挂”“多场景收纳”“展开使用”，要求字形清楚；不得出现其他标语、功效、尺寸、价格、认证、联系方式、平台Logo或水印。不得出现手、咖啡杯和袋内垃圾。""",
    }


def main() -> Path:
    started_all = time.monotonic()
    successes, failures = cutout_all()
    unique, duplicates = deduplicate(successes)
    if not unique:
        raise RuntimeError("没有成功生成可用抠图")
    sheets, mapping = make_evidence_sheets(unique)
    identity = unique[0]
    model_inputs = [identity["path"], *sheets]
    prompt_map = prompts(identity["asset_id"], len(unique), len(sheets))
    report = {
        "product_id": PRODUCT_ID, "model": MODEL,
        "selection_rule": "downloaded + >160px + not excluded/merged + semantic suitable",
        "source_asset_count": len(successes) + len(failures),
        "cutout_success_count": len(successes), "cutout_failure_count": len(failures),
        "cutout_reused_count": sum(1 for item in successes if item["reused"]),
        "cutout_created_count": sum(1 for item in successes if not item["reused"]),
        "unique_cutout_count": len(unique), "duplicate_cutout_count": len(duplicates),
        "identity_asset_id": identity["asset_id"],
        "cutouts": [{key: value for key, value in item.items() if key != "path"}
                    for item in successes],
        "failures": failures, "duplicates": duplicates, "evidence_mapping": mapping,
        "model_inputs": [str(path.relative_to(ROOT)).replace("\\", "/") for path in model_inputs],
        "items": [],
    }
    for name, prompt in prompt_map.items():
        started = time.monotonic()
        item = {"name": name, "prompt": prompt}
        try:
            data = generate_candidate(prompt, model_inputs, MODEL)
            target = OUTPUT / f"{name}.png"
            target.write_bytes(data)
            with Image.open(io.BytesIO(data)) as image:
                dimensions = [image.width, image.height]
            item.update(status="success", file=str(target.relative_to(ROOT)).replace("\\", "/"),
                        sha256=hashlib.sha256(data).hexdigest(), dimensions=dimensions)
        except Exception as exc:
            item.update(status="failed", error_type=type(exc).__name__, error=str(exc)[:500])
        item["elapsed_seconds"] = round(time.monotonic() - started, 2)
        report["items"].append(item)
        print(name, item["status"], item["elapsed_seconds"], flush=True)
    report["total_elapsed_seconds"] = round(time.monotonic() - started_all, 2)
    path = OUTPUT / "report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


if __name__ == "__main__":
    print(main())
