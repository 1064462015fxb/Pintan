"""备份实际SQLite文件后执行采集映射迁移；打印数量，不输出素材或密钥。"""
from datetime import datetime, timezone
from pathlib import Path
import json
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from pintan.database import init_database, engine
from sqlalchemy import text

def main():
    database=ROOT/'data/pintan.db'
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup=ROOT/'data/backups'/f'pintan-before-collector-v5-{stamp}.db'
    if database.exists():
        backup.parent.mkdir(parents=True,exist_ok=True)
        # SQLite备份API把WAL中已提交数据也复制进去，可安全处理正在运行的服务。
        with sqlite3.connect(str(database)) as source, sqlite3.connect(str(backup)) as target:
            source.backup(target)
    init_database(ROOT)
    with engine(ROOT).connect() as c:
        counts={name:c.execute(text('SELECT COUNT(*) FROM '+name)).scalar_one() for name in
            ['offer_snapshots','fact_evidence','offer_fields','offer_price_tiers','supplier_snapshots','pain_points','asset_requirements']}
        checks=c.execute(text('PRAGMA foreign_key_check')).all()
    report={'backup':str(backup),'counts':counts,'foreign_key_violations':len(checks)}
    print(json.dumps(report,ensure_ascii=True))
    if checks:raise RuntimeError('数据库外键核验失败，请查看备份后排查')

if __name__=='__main__':main()
