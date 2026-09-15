"""三张业务 CSV 的读取、校验和安全写入。

本模块故意保持 CSV 为业务事实来源。模型调用快照、运行状态和报告只写入
``outputs/*.json``，避免把运行审计数据误认为第四张业务表。
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


STABLE_FILE = "pintan_stable_products.csv"
TREND_FILE = "pintan_market_trending_products.csv"
SEARCH_FILE = "pintan_trend_search_results.csv"


def read_csv_raw(path: Path) -> list[dict[str, str]]:
    if not path.exists(): raise FileNotFoundError(f"数据文件不存在: {path.name}；请先运行 init-demo")
    with path.open("r", encoding="utf-8-sig", newline="") as handle: return list(csv.DictReader(handle))

def read_csv(path: Path) -> list[dict[str, str]]:
    from .database import BUSINESS, initialized, read_business
    if path.name in BUSINESS and initialized(path.parent):
        return read_business(path.parent, path.name)
    return read_csv_raw(path)


def atomic_write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    from .database import BUSINESS, initialized, write_business
    if path.name in BUSINESS and initialized(path.parent):
        write_business(path.parent,path.name,rows); return
    atomic_write_csv_raw(path,rows,fields); return

def atomic_write_csv_raw(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    """仅供首次演示表生成与显式导出使用。"""
    if not rows and not fields:
        raise ValueError("空表必须显式提供字段")
    path.parent.mkdir(parents=True, exist_ok=True)
    names = fields or list(rows[0])
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=names, extrasaction="ignore")
            writer.writeheader(); writer.writerows(rows)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def validate_three_tables(root: Path) -> dict[str, int]:
    stable, trends, searches = (read_csv(root / name) for name in (STABLE_FILE, TREND_FILE, SEARCH_FILE))
    for label, rows, key in (("稳定", stable, "stable_id"), ("爆款", trends, "trend_id"), ("搜索", searches, "search_id")):
        ids = [r.get(key, "") for r in rows]
        if not rows or "" in ids or len(ids) != len(set(ids)):
            raise ValueError(f"{label}表为空、主键缺失或重复")
    trend_ids = {r["trend_id"] for r in trends}
    counts = Counter(r.get("trend_id") for r in searches)
    if set(counts) != trend_ids or any(counts[k] != 10 for k in trend_ids):
        raise ValueError("每个爆款必须且只能关联 10 条搜索候选")
    for row in stable + searches:
        if row.get("verification_status") != "待复核" or row.get("data_origin") != "model_processed_demo":
            raise ValueError("演示商品必须标记 model_processed_demo / 待复核")
    return {"stable_products": len(stable), "trends": len(trends), "search_results": len(searches)}
