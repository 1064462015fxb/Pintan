"""内容生产前置状态与门禁。

批量选品只保存文字证据；这个模块把二阶段素材采集、事实确认和
图片授权汇总为一个可恢复的状态，避免前端绕过必要步骤。
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from .asset_records import metadata, resolve
from .collector_mapping import COMMON_FIELDS, read_mapping
from .database import (AssetAnalysis, AssetAuthorization, CollectionRun, CollectorTask, Product,
                       ProductAsset, ProductClaim, session_scope)

ACTIVE = {"queued", "opening", "reading", "waiting_user", "normalizing", "downloading"}


def _usable_asset(session, asset: ProductAsset) -> bool:
    meta = metadata(session, asset.asset_id)
    effective = meta.get("review_asset_type", asset.asset_type)
    return bool(
        asset.local_path and asset.sha256
        and asset.status != "复核排除"
        and not asset.status.startswith("已合并:")
        and meta.get("review_included", True) is not False
        and not (effective == "待分类商品图" and max(asset.width or 0, asset.height or 0) <= 256)
    )


def readiness(root: Path, product_id: str) -> dict:
    with session_scope(root) as s:
        product = s.get(Product, product_id)
        if not product:
            raise ValueError("商品不存在")
        tasks = list(s.scalars(select(CollectorTask).where(
            CollectorTask.offer_id == product_id.removeprefix("1688-")
        ).order_by(CollectorTask.created_at.desc())))
        asset_task = next((t for t in tasks if json.loads(t.payload or "{}").get("mode") == "assets"), None)
        task_data = json.loads(asset_task.payload or "{}") if asset_task else {}
        run_id = task_data.get("run_id")
        run = s.get(CollectionRun, run_id) if run_id else None
        mapping = read_mapping(s, run_id) if run else None
        review_fields = (mapping or {}).get("review_fields", [])
        present = {x.get("field_name") for x in review_fields if x.get("included") and str(x.get("value") or "").strip()}
        missing = sorted(COMMON_FIELDS - present)
        claims = list(s.scalars(select(ProductClaim).where(ProductClaim.product_id == product_id)))

        seen: set[str] = set()
        usable: list[ProductAsset] = []
        discovered = 0
        downloaded = 0
        for raw in s.scalars(select(ProductAsset).where(ProductAsset.product_id == product_id)):
            asset = resolve(s, raw)
            if asset.asset_id in seen:
                continue
            seen.add(asset.asset_id)
            discovered += 1
            downloaded += int(bool(asset.local_path))
            if _usable_asset(s, asset):
                usable.append(asset)
        authorized = 0
        semantic = {}
        for analysis in s.scalars(select(AssetAnalysis).join(ProductAsset).where(
                ProductAsset.product_id == product_id)):
            try:
                payload = json.loads(analysis.payload or "{}")
            except ValueError:
                payload = {}
            semantic[analysis.asset_id] = {
                "status": analysis.status,
                "primary_type": payload.get("primary_type"),
                "tags": payload.get("tags") or [],
                "confidence": payload.get("confidence"),
                "reason": payload.get("reason") or payload.get("error_type"),
                "ocr_status": payload.get("ocr_status"),
            }
        for asset in usable:
            auth = s.scalar(select(AssetAuthorization).where(
                AssetAuthorization.asset_id == asset.asset_id,
                AssetAuthorization.asset_sha256 == asset.sha256,
                AssetAuthorization.confirmed == True))  # noqa: E712
            authorized += int(bool(auth))

        facts_confirmed = bool(run and run.status == "已人工复核" and claims and not missing)
        task_status = asset_task.status if asset_task else None
        blockers = []
        if not product.source_product_url:
            blockers.append({"code": "missing_source_url", "message": "商品没有1688详情地址，无法采集素材"})
        elif not asset_task:
            blockers.append({"code": "assets_not_collected", "message": "尚未进行二阶段文字与图片采集"})
        elif task_status in ACTIVE:
            blockers.append({"code": "assets_task_running", "message": task_data.get("message") or "素材采集正在进行"})
        elif task_status in {"failed", "cancelled", "interrupted"}:
            blockers.append({"code": "assets_task_failed", "message": task_data.get("message") or "素材采集未完成"})
        if asset_task and not run:
            blockers.append({"code": "missing_collection_run", "message": "素材任务尚未形成可复核的采集记录"})
        elif run and not facts_confirmed:
            message = "请补齐并确认商品通用属性" if missing else "请人工复核并确认商品事实"
            blockers.append({"code": "facts_review_required", "message": message})
        if not usable:
            blockers.append({"code": "missing_usable_images", "message": "没有已下载且未被排除的有效图片"})
        elif authorized < len(usable):
            blockers.append({"code": "asset_authorization_required", "message": f"请确认当前商品图片使用权（{authorized}/{len(usable)}）"})

        next_action = ("prepare" if not asset_task or not run or not usable or task_status in {"failed", "cancelled", "interrupted"}
                       else "wait" if task_status in ACTIVE
                       else "review_facts" if not facts_confirmed
                       else "authorize_assets" if authorized < len(usable)
                       else "generate_content")
        return {
            "product_id": product_id,
            "source_url": product.source_product_url,
            "ready": not blockers,
            "next_action": next_action,
            "blockers": blockers,
            "task": ({"task_id": asset_task.task_id, "status": task_status,
                      "status_label": task_data.get("message") or task_status,
                      "found": task_data.get("found", 0), "downloaded": task_data.get("downloaded", 0),
                      "failed": task_data.get("failed", 0), "run_id": run_id,
                      "error_type": task_data.get("error_type"),
                      "fact_completion": task_data.get("fact_completion")} if asset_task else None),
            "facts": {"run_id": run_id, "confirmed": facts_confirmed, "claim_count": len(claims),
                      "missing_required": missing, "conflicts": (mapping or {}).get("conflicts", []),
                      "anomalies": (mapping or {}).get("anomalies", []),
                      "catalog": (mapping or {}).get("catalog", []),
                      "review_fields": review_fields},
            "assets": {"discovered": discovered, "downloaded": downloaded,
                       "usable": len(usable), "authorized": authorized,
                       "all_authorized": bool(usable) and authorized == len(usable),
                       "items": [{"asset_id": a.asset_id, "asset_type": metadata(s, a.asset_id).get("review_asset_type", a.asset_type),
                                  "status": a.status, "width": a.width, "height": a.height,
                                  "authorization_status": "已确认" if s.scalar(select(AssetAuthorization).where(
                                      AssetAuthorization.asset_id == a.asset_id,
                                      AssetAuthorization.asset_sha256 == a.sha256,
                                      AssetAuthorization.confirmed == True)) else "未确认",  # noqa: E712
                                  "semantic_classification": semantic.get(a.asset_id),
                                  "preview_url": f"/api/assets/{a.asset_id}/file"} for a in usable]}
        }


def require_ready(root: Path, product_id: str, stage: str = "content") -> dict:
    state = readiness(root, product_id)
    if not state["ready"]:
        error = ValueError("内容生产前置条件未完成")
        error.readiness = {"code": "content_not_ready", "stage": stage,
                           "blockers": state["blockers"], "next_action": state["next_action"]}
        raise error
    return state
