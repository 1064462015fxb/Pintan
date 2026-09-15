"""SQLite 主仓库：业务表保存完整快照，关系表保存可审计的素材与母版。"""
from __future__ import annotations
import csv, hashlib, json, sqlite3
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import NullPool

SCHEMA_VERSION = 7
BUSINESS = {"pintan_stable_products.csv": ("stable", "stable_id"), "pintan_market_trending_products.csv": ("trend", "trend_id"), "pintan_trend_search_results.csv": ("search", "search_id")}

class Base(DeclarativeBase): pass
class SchemaMeta(Base):
    __tablename__="schema_meta"; key:Mapped[str]=mapped_column(String,primary_key=True); value:Mapped[str]=mapped_column(String)
class BusinessRow(Base):
    __tablename__="business_rows"; kind:Mapped[str]=mapped_column(String,primary_key=True); row_id:Mapped[str]=mapped_column(String,primary_key=True); payload:Mapped[str]=mapped_column(Text)
class Product(Base):
    __tablename__="products"; product_id:Mapped[str]=mapped_column(String,primary_key=True); title:Mapped[str]=mapped_column(Text); category:Mapped[str]=mapped_column(String); source_platform:Mapped[str]=mapped_column(String,default="1688"); source_product_url:Mapped[str|None]=mapped_column(Text,nullable=True); verification_status:Mapped[str]=mapped_column(String,default="待复核")
class TrendSignal(Base):
    __tablename__="trend_signals"; trend_id:Mapped[str]=mapped_column(String,primary_key=True); payload:Mapped[str]=mapped_column(Text)
class SearchCandidate(Base):
    __tablename__="search_candidates"; search_id:Mapped[str]=mapped_column(String,primary_key=True); trend_id:Mapped[str]=mapped_column(ForeignKey("trend_signals.trend_id")); product_id:Mapped[str]=mapped_column(ForeignKey("products.product_id")); payload:Mapped[str]=mapped_column(Text)
class StableScore(Base):
    __tablename__="stable_scores"; product_id:Mapped[str]=mapped_column(ForeignKey("products.product_id"),primary_key=True); payload:Mapped[str]=mapped_column(Text)
class TrendMatch(Base):
    __tablename__="trend_matches"; search_id:Mapped[str]=mapped_column(ForeignKey("search_candidates.search_id"),primary_key=True); payload:Mapped[str]=mapped_column(Text)
class ProductAsset(Base):
    __tablename__="product_assets"; asset_id:Mapped[str]=mapped_column(String,primary_key=True); product_id:Mapped[str]=mapped_column(ForeignKey("products.product_id")); asset_type:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String,default="待采集"); source_url:Mapped[str|None]=mapped_column(Text,nullable=True); local_path:Mapped[str|None]=mapped_column(Text,nullable=True); cutout_path:Mapped[str|None]=mapped_column(Text,nullable=True); sha256:Mapped[str|None]=mapped_column(String,nullable=True); mime_type:Mapped[str|None]=mapped_column(String,nullable=True); byte_size:Mapped[int|None]=mapped_column(Integer,nullable=True); width:Mapped[int|None]=mapped_column(Integer,nullable=True); height:Mapped[int|None]=mapped_column(Integer,nullable=True); collected_at:Mapped[str|None]=mapped_column(String,nullable=True); ocr_text:Mapped[str|None]=mapped_column(Text,nullable=True); authorization_status:Mapped[str|None]=mapped_column(String,nullable=True)
class AssetCaptureMeta(Base):
    """图片来源、别名和最近下载尝试；不改动历史素材外键。"""
    __tablename__='asset_capture_meta'
    asset_id:Mapped[str]=mapped_column(ForeignKey('product_assets.asset_id'),primary_key=True)
    payload:Mapped[str]=mapped_column(Text,default='{}')

class AssetAnalysis(Base):
    """原图的语义分类；网页区域分类及人工复核记录保持独立。"""
    __tablename__='asset_analyses'
    asset_id:Mapped[str]=mapped_column(ForeignKey('product_assets.asset_id'),primary_key=True)
    input_hash:Mapped[str]=mapped_column(String)
    status:Mapped[str]=mapped_column(String)
    payload:Mapped[str]=mapped_column(Text,default='{}')
    updated_at:Mapped[str]=mapped_column(String)

class ImagePlan(Base):
    """平台图片编排：顺序、用途和原图引用，不修改原图类别。"""
    __tablename__='image_plans'
    plan_id:Mapped[str]=mapped_column(String,primary_key=True)
    content_id:Mapped[str]=mapped_column(ForeignKey('platform_contents.content_id'))
    input_hash:Mapped[str]=mapped_column(String)
    payload:Mapped[str]=mapped_column(Text)
    created_at:Mapped[str]=mapped_column(String)

class ImageReview(Base):
    """逐轮生成结果和独立视觉审核；最多三轮，不覆盖失败证据。"""
    __tablename__='image_reviews'
    review_id:Mapped[str]=mapped_column(String,primary_key=True)
    plan_id:Mapped[str]=mapped_column(ForeignKey('image_plans.plan_id'))
    slot:Mapped[str]=mapped_column(String)
    attempt:Mapped[int]=mapped_column(Integer)
    payload:Mapped[str]=mapped_column(Text)
    created_at:Mapped[str]=mapped_column(String)

class AssetAuthorization(Base):
    """素材使用权确认。与原图哈希绑定，更换图片后不会沿用旧授权。"""
    __tablename__="asset_authorizations"; authorization_id:Mapped[str]=mapped_column(String,primary_key=True); asset_id:Mapped[str]=mapped_column(ForeignKey("product_assets.asset_id")); asset_sha256:Mapped[str]=mapped_column(String); confirmed:Mapped[bool]=mapped_column(Boolean,default=False); statement:Mapped[str]=mapped_column(Text); confirmed_at:Mapped[str]=mapped_column(String); __table_args__=(UniqueConstraint("asset_id","asset_sha256"),)
class GeneratedMedia(Base):
    """图片、音频和视频的统一版本与血缘记录。"""
    __tablename__="generated_media"; media_id:Mapped[str]=mapped_column(String,primary_key=True); product_id:Mapped[str]=mapped_column(ForeignKey("products.product_id")); content_id:Mapped[str|None]=mapped_column(ForeignKey("platform_contents.content_id"),nullable=True); media_type:Mapped[str]=mapped_column(String); slot:Mapped[str]=mapped_column(String); version:Mapped[int]=mapped_column(Integer,default=1); status:Mapped[str]=mapped_column(String); local_path:Mapped[str|None]=mapped_column(Text,nullable=True); sha256:Mapped[str|None]=mapped_column(String,nullable=True); source_asset_id:Mapped[str|None]=mapped_column(ForeignKey("product_assets.asset_id"),nullable=True); source_sha256:Mapped[str|None]=mapped_column(String,nullable=True); metadata_json:Mapped[str]=mapped_column(Text,default="{}"); confirmed:Mapped[bool]=mapped_column(Boolean,default=False); created_at:Mapped[str]=mapped_column(String); __table_args__=(UniqueConstraint("product_id","content_id","media_type","slot","version"),)
class MediaTask(Base):
    __tablename__="media_tasks"; task_id:Mapped[str]=mapped_column(String,primary_key=True); kind:Mapped[str]=mapped_column(String); product_id:Mapped[str]=mapped_column(String); content_id:Mapped[str|None]=mapped_column(String,nullable=True); input_hash:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String); result_json:Mapped[str]=mapped_column(Text,default="{}"); error_type:Mapped[str|None]=mapped_column(String,nullable=True); created_at:Mapped[str]=mapped_column(String); updated_at:Mapped[str]=mapped_column(String)
class ProductClaim(Base):
    __tablename__="product_claims"; claim_id:Mapped[str]=mapped_column(String,primary_key=True); product_id:Mapped[str]=mapped_column(ForeignKey("products.product_id")); field_name:Mapped[str]=mapped_column(String); value:Mapped[str]=mapped_column(Text); source_field:Mapped[str]=mapped_column(String); derivation_rule:Mapped[str]=mapped_column(String); confidence:Mapped[float]=mapped_column(Float); verification_status:Mapped[str]=mapped_column(String,default="待复核"); __table_args__=(UniqueConstraint("product_id","field_name","value"),)
class CollectionRun(Base):
    """一次1688采集的审计记录；快照不包含Cookie或请求头。"""
    __tablename__="collection_runs"; run_id:Mapped[str]=mapped_column(String,primary_key=True); platform:Mapped[str]=mapped_column(String,default="1688"); page_url:Mapped[str]=mapped_column(Text); offer_id:Mapped[str]=mapped_column(String); product_id:Mapped[str|None]=mapped_column(ForeignKey("products.product_id"),nullable=True); status:Mapped[str]=mapped_column(String); raw_snapshot_json:Mapped[str]=mapped_column(Text); normalized_json:Mapped[str]=mapped_column(Text,default="{}"); model:Mapped[str]=mapped_column(String,default="rules"); prompt_version:Mapped[str]=mapped_column(String,default="collector-v1.0"); error_type:Mapped[str|None]=mapped_column(String,nullable=True); created_at:Mapped[str]=mapped_column(String); updated_at:Mapped[str]=mapped_column(String)
class CollectedFact(Base):
    """原值与规范值并存，确保每个字段均能回到页面证据。"""
    __tablename__="collected_facts"; fact_id:Mapped[str]=mapped_column(String,primary_key=True); run_id:Mapped[str]=mapped_column(ForeignKey("collection_runs.run_id")); product_id:Mapped[str]=mapped_column(ForeignKey("products.product_id")); field_name:Mapped[str]=mapped_column(String); raw_value:Mapped[str]=mapped_column(Text); normalized_value:Mapped[str]=mapped_column(Text); source_locator:Mapped[str]=mapped_column(Text); confidence:Mapped[float]=mapped_column(Float); verification_status:Mapped[str]=mapped_column(String,default="待复核")
class CollectedSku(Base):
    __tablename__="collected_skus"; sku_id:Mapped[str]=mapped_column(String,primary_key=True); run_id:Mapped[str]=mapped_column(ForeignKey("collection_runs.run_id")); product_id:Mapped[str]=mapped_column(ForeignKey("products.product_id")); spec_json:Mapped[str]=mapped_column(Text); price_cny:Mapped[float|None]=mapped_column(Float,nullable=True); stock:Mapped[int|None]=mapped_column(Integer,nullable=True); image_url:Mapped[str|None]=mapped_column(Text,nullable=True); verification_status:Mapped[str]=mapped_column(String,default="待复核")
class OfferSnapshot(Base):
    """每次采集的标准货源快照；与原始证据一起保留，重新采集不覆盖历史。"""
    __tablename__='offer_snapshots'
    run_id:Mapped[str]=mapped_column(ForeignKey('collection_runs.run_id'),primary_key=True)
    product_id:Mapped[str]=mapped_column(ForeignKey('products.product_id'),index=True)
    mapping_version:Mapped[str]=mapped_column(String)
    payload:Mapped[str]=mapped_column(Text)

class FactEvidence(Base):
    __tablename__='fact_evidence'
    evidence_id:Mapped[str]=mapped_column(String,primary_key=True)
    run_id:Mapped[str]=mapped_column(ForeignKey('collection_runs.run_id'),index=True)
    region:Mapped[str]=mapped_column(String)
    source_locator:Mapped[str]=mapped_column(Text)
    raw_text:Mapped[str]=mapped_column(Text)

class OfferMappingHistory(Base):
    """映射规则升级时保留上一个标准化版本，原始页面快照始终不变。"""
    __tablename__='offer_mapping_history'
    run_id:Mapped[str]=mapped_column(ForeignKey('collection_runs.run_id'),primary_key=True)
    mapping_version:Mapped[str]=mapped_column(String,primary_key=True)
    payload:Mapped[str]=mapped_column(Text)

class OfferField(Base):
    """一个字段可保留多个候选值；冲突不会由最后一项覆盖。"""
    __tablename__='offer_fields'
    field_id:Mapped[str]=mapped_column(String,primary_key=True)
    run_id:Mapped[str]=mapped_column(ForeignKey('offer_snapshots.run_id'),index=True)
    field_name:Mapped[str]=mapped_column(String)
    value:Mapped[str]=mapped_column(Text)
    evidence_id:Mapped[str]=mapped_column(ForeignKey('fact_evidence.evidence_id'))
    status:Mapped[str]=mapped_column(String,default='待复核')

class OfferPriceTier(Base):
    __tablename__='offer_price_tiers'
    tier_id:Mapped[str]=mapped_column(String,primary_key=True)
    run_id:Mapped[str]=mapped_column(ForeignKey('offer_snapshots.run_id'),index=True)
    evidence_id:Mapped[str]=mapped_column(ForeignKey('fact_evidence.evidence_id'))
    payload:Mapped[str]=mapped_column(Text)

class SupplierSnapshot(Base):
    __tablename__='supplier_snapshots'
    run_id:Mapped[str]=mapped_column(ForeignKey('offer_snapshots.run_id'),primary_key=True)
    payload:Mapped[str]=mapped_column(Text)

class PainPoint(Base):
    """评价片段仅为痛点候选；没有样本分母时不计算频率。"""
    __tablename__='pain_points'
    pain_id:Mapped[str]=mapped_column(String,primary_key=True)
    run_id:Mapped[str]=mapped_column(ForeignKey('offer_snapshots.run_id'),index=True)
    evidence_id:Mapped[str]=mapped_column(ForeignKey('fact_evidence.evidence_id'))
    text:Mapped[str]=mapped_column(Text)
    status:Mapped[str]=mapped_column(String,default='待复核候选')

class AssetRequirement(Base):
    """需求独立于真实素材，允许同类多图。满足情况由文件及用途关联判断。"""
    __tablename__='asset_requirements'
    product_id:Mapped[str]=mapped_column(ForeignKey('products.product_id'),primary_key=True)
    asset_type:Mapped[str]=mapped_column(String,primary_key=True)
    purpose:Mapped[str]=mapped_column(String,default='内容生产')

class CollectorHelper(Base):
    """只存本机配对令牌的哈希，不保存任何1688登录信息。"""
    __tablename__="collector_helpers"
    helper_id:Mapped[str]=mapped_column(String,primary_key=True)
    token_hash:Mapped[str]=mapped_column(String)
    extension_origin:Mapped[str]=mapped_column(String)
    name:Mapped[str]=mapped_column(String)
    version:Mapped[str]=mapped_column(String)
    connected:Mapped[bool]=mapped_column(Boolean,default=False)
    last_seen:Mapped[str]=mapped_column(String)
class CollectorTask(Base):
    """采集任务跨重启保存；payload仅含白名单快照、阶段和逐图结果。"""
    __tablename__="collector_tasks"
    task_id:Mapped[str]=mapped_column(String,primary_key=True)
    helper_id:Mapped[str]=mapped_column(ForeignKey("collector_helpers.helper_id"))
    offer_id:Mapped[str]=mapped_column(String,index=True)
    page_url:Mapped[str]=mapped_column(Text)
    status:Mapped[str]=mapped_column(String,index=True)
    payload:Mapped[str]=mapped_column(Text,default="{}")
    created_at:Mapped[str]=mapped_column(String)
    updated_at:Mapped[str]=mapped_column(String)
class BatchSelection(Base):
    """一次关键词批量选品；搜索、详情采集和评分状态分别保存在payload。"""
    __tablename__="batch_selections"
    batch_id:Mapped[str]=mapped_column(String,primary_key=True)
    helper_id:Mapped[str|None]=mapped_column(ForeignKey("collector_helpers.helper_id"),nullable=True)
    original_keyword:Mapped[str]=mapped_column(String)
    normalized_keyword:Mapped[str]=mapped_column(String)
    filters_json:Mapped[str]=mapped_column(Text,default="{}")
    target_count:Mapped[int]=mapped_column(Integer,default=20)
    max_candidates:Mapped[int]=mapped_column(Integer,default=60)
    status:Mapped[str]=mapped_column(String,index=True)
    payload:Mapped[str]=mapped_column(Text,default="{}")
    created_at:Mapped[str]=mapped_column(String)
    updated_at:Mapped[str]=mapped_column(String)
class BatchSelectionItem(Base):
    """搜索候选及其详情采集/评分结果；失败项不会污染稳定池。"""
    __tablename__="batch_selection_items"
    batch_id:Mapped[str]=mapped_column(ForeignKey("batch_selections.batch_id"),primary_key=True)
    offer_id:Mapped[str]=mapped_column(String,primary_key=True)
    relevance_rank:Mapped[int]=mapped_column(Integer)
    title:Mapped[str]=mapped_column(Text,default="")
    page_url:Mapped[str]=mapped_column(Text)
    status:Mapped[str]=mapped_column(String,index=True)
    collector_task_id:Mapped[str|None]=mapped_column(ForeignKey("collector_tasks.task_id"),nullable=True)
    product_id:Mapped[str|None]=mapped_column(ForeignKey("products.product_id"),nullable=True)
    error_type:Mapped[str|None]=mapped_column(String,nullable=True)
    payload:Mapped[str]=mapped_column(Text,default="{}")
class ContentMaster(Base):
    __tablename__="content_masters"; master_id:Mapped[str]=mapped_column(String,primary_key=True); product_id:Mapped[str]=mapped_column(ForeignKey("products.product_id")); trend_id:Mapped[str|None]=mapped_column(String,nullable=True); cache_key:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String); current_version:Mapped[int]=mapped_column(Integer,default=0); created_at:Mapped[str]=mapped_column(String)
class ContentVersion(Base):
    __tablename__="content_versions"; id:Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True); master_id:Mapped[str]=mapped_column(ForeignKey("content_masters.master_id")); version:Mapped[int]=mapped_column(Integer); content_json:Mapped[str]=mapped_column(Text); status:Mapped[str]=mapped_column(String,default="草稿"); model:Mapped[str]=mapped_column(String); prompt_version:Mapped[str]=mapped_column(String); created_at:Mapped[str]=mapped_column(String); __table_args__=(UniqueConstraint("master_id","version"),)
class ContentEvidence(Base):
    __tablename__="content_evidence"; id:Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True); version_id:Mapped[int]=mapped_column(ForeignKey("content_versions.id")); node_path:Mapped[str]=mapped_column(String); claim_id:Mapped[str]=mapped_column(ForeignKey("product_claims.claim_id"))
class AgentTask(Base):
    __tablename__="agent_tasks"; task_id:Mapped[str]=mapped_column(String,primary_key=True); kind:Mapped[str]=mapped_column(String); subject_id:Mapped[str]=mapped_column(String); status:Mapped[str]=mapped_column(String); error_type:Mapped[str|None]=mapped_column(String,nullable=True); created_at:Mapped[str]=mapped_column(String); updated_at:Mapped[str]=mapped_column(String)
class AngleDiagnosis(Base):
    __tablename__="angle_diagnoses"; diagnosis_id:Mapped[str]=mapped_column(String,primary_key=True); product_id:Mapped[str]=mapped_column(ForeignKey("products.product_id")); trend_id:Mapped[str|None]=mapped_column(String,nullable=True); cache_key:Mapped[str]=mapped_column(String,unique=True); result_json:Mapped[str]=mapped_column(Text); model:Mapped[str]=mapped_column(String); prompt_version:Mapped[str]=mapped_column(String); created_at:Mapped[str]=mapped_column(String)
class ContentChoice(Base):
    __tablename__="content_choices"; choice_id:Mapped[str]=mapped_column(String,primary_key=True); diagnosis_id:Mapped[str]=mapped_column(ForeignKey("angle_diagnoses.diagnosis_id")); selected_json:Mapped[str]=mapped_column(Text); custom_angle:Mapped[str]=mapped_column(Text,default=""); created_at:Mapped[str]=mapped_column(String)
class PlatformRule(Base):
    __tablename__="platform_rules"; rule_id:Mapped[str]=mapped_column(String,primary_key=True); platform:Mapped[str]=mapped_column(String); version:Mapped[str]=mapped_column(String); source_type:Mapped[str]=mapped_column(String); source_url:Mapped[str|None]=mapped_column(Text,nullable=True); verified_at:Mapped[str]=mapped_column(String); rules_json:Mapped[str]=mapped_column(Text)
class PlatformContent(Base):
    __tablename__="platform_contents"; content_id:Mapped[str]=mapped_column(String,primary_key=True); master_id:Mapped[str]=mapped_column(ForeignKey("content_masters.master_id")); choice_id:Mapped[str]=mapped_column(ForeignKey("content_choices.choice_id")); platform:Mapped[str]=mapped_column(String); market:Mapped[str]=mapped_column(String); cache_key:Mapped[str]=mapped_column(String); current_version:Mapped[int]=mapped_column(Integer,default=1); risk_level:Mapped[str]=mapped_column(String); created_at:Mapped[str]=mapped_column(String)
class PlatformContentVersion(Base):
    __tablename__="platform_content_versions"; id:Mapped[int]=mapped_column(Integer,primary_key=True,autoincrement=True); content_id:Mapped[str]=mapped_column(ForeignKey("platform_contents.content_id")); version:Mapped[int]=mapped_column(Integer); visible_json:Mapped[str]=mapped_column(Text); issues_json:Mapped[str]=mapped_column(Text); changes_json:Mapped[str]=mapped_column(Text); status:Mapped[str]=mapped_column(String,default="草稿"); model:Mapped[str]=mapped_column(String); created_at:Mapped[str]=mapped_column(String); __table_args__=(UniqueConstraint("content_id","version"),)

def utcnow(): return datetime.now(timezone.utc).isoformat(timespec="seconds")
def db_path(root:Path): return root/"data/pintan.db"
def engine(root:Path):
    path=db_path(root); path.parent.mkdir(parents=True,exist_ok=True)
    return create_engine(f"sqlite:///{path.as_posix()}",connect_args={"check_same_thread":False},poolclass=NullPool)
@contextmanager
def session_scope(root:Path):
    s=sessionmaker(engine(root),expire_on_commit=False)()
    try:
        from .task_control import publication, checkpoint
        checkpoint()
        yield s
        with publication():
            s.commit()
    except: s.rollback(); raise
    finally: s.close()
def initialized(root:Path): return db_path(root).exists()

def set_data_mode(root:Path, mode:str) -> None:
    """切换数据初始化模式；manual 模式禁止启动时重新灌入演示 CSV。"""
    if mode not in {"manual","demo"}:raise ValueError("未知数据模式")
    Base.metadata.create_all(engine(root))
    with session_scope(root) as s:
        row=s.get(SchemaMeta,"data_mode")
        if row:row.value=mode
        else:s.add(SchemaMeta(key="data_mode",value=mode))

def clear_database_content(root:Path) -> Path:
    """备份后清空全部业务内容，并进入不会自动回灌 CSV 的手动模式。"""
    path=db_path(root)
    path.parent.mkdir(parents=True,exist_ok=True)
    if not path.exists():Base.metadata.create_all(engine(root))
    backup_dir=path.parent/"backups";backup_dir.mkdir(parents=True,exist_ok=True)
    stamp=datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup=backup_dir/f"pintan-before-manual-clear-{stamp}.db"
    with closing(sqlite3.connect(path)) as source, closing(sqlite3.connect(backup)) as target:
        source.backup(target)
    eng=engine(root)
    with eng.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
        connection.execute(SchemaMeta.__table__.insert(),[
            {"key":"version","value":str(SCHEMA_VERSION)},
            {"key":"data_mode","value":"manual"},
        ])
        connection.execute(text("PRAGMA foreign_keys=ON"))
    return backup

def _raw_csv(path:Path):
    if not path.exists(): raise FileNotFoundError(path.name)
    with path.open("r",encoding="utf-8-sig",newline="") as f: return list(csv.DictReader(f))

def init_database(root:Path, csv_reader=None) -> dict[str,int]:
    eng=engine(root); Base.metadata.create_all(eng)
    # SQLite 的 create_all 不会给旧表补列，因此用只增不删的轻量迁移保持历史库可用。
    asset_columns={x["name"] for x in inspect(eng).get_columns("product_assets")}
    additions={"cutout_path":"TEXT","mime_type":"VARCHAR","byte_size":"INTEGER","width":"INTEGER",
               "height":"INTEGER","collected_at":"VARCHAR"}
    with eng.begin() as connection:
        for name,sql_type in additions.items():
            if name not in asset_columns:connection.execute(text(f"ALTER TABLE product_assets ADD COLUMN {name} {sql_type}"))
    counts={}
    with session_scope(root) as s:
        meta=s.get(SchemaMeta,"version")
        if meta and int(meta.value)>SCHEMA_VERSION: raise RuntimeError("数据库版本高于当前程序")
        if not meta: s.add(SchemaMeta(key="version",value=str(SCHEMA_VERSION)))
        elif int(meta.value)<SCHEMA_VERSION:meta.value=str(SCHEMA_VERSION)
        mode=s.get(SchemaMeta,"data_mode")
        if mode and mode.value=="manual":
            # 手工数据模式下，服务启动只建表/迁移，绝不从兼容 CSV 回灌。
            return {"mode":"manual","stable":0,"trend":0,"search":0}
        for filename,(kind,key) in BUSINESS.items():
            rows=_raw_csv(root/filename); counts[kind]=len(rows)
            marker=s.get(SchemaMeta,"import:"+filename)
            if marker: continue
            existing={x.row_id:x.payload for x in s.scalars(select(BusinessRow).where(BusinessRow.kind==kind))}
            for row in rows:
                payload=json.dumps(row,ensure_ascii=False,sort_keys=True)
                if row[key] in existing and existing[row[key]]!=payload: raise ValueError(f"数据库与CSV冲突: {filename}/{row[key]}")
                if row[key] not in existing: s.add(BusinessRow(kind=kind,row_id=row[key],payload=payload))
            s.add(SchemaMeta(key="import:"+filename,value=hashlib.sha256("".join(sorted(existing.values())+[json.dumps(r,ensure_ascii=False,sort_keys=True) for r in rows if r[key] not in existing]).encode()).hexdigest()))
        stable=_raw_csv(root/"pintan_stable_products.csv")
        fields=["title","category","wholesale_price_cny","estimated_shipping_cny","material","battery_capacity_mah","power_w","feature_tags","color_options","supplier_quality_proxy_pct"]
        for row in stable:
            pid=row["stable_id"]
            if not s.get(Product,pid): s.add(Product(product_id=pid,title=row["title"],category=row["category"],verification_status=row.get("verification_status","待复核")))
            for f in fields:
                if row.get(f,"")!="":
                    cid="CL-"+hashlib.sha256(f"{pid}|{f}|{row[f]}".encode()).hexdigest()[:16]
                    if not s.get(ProductClaim,cid): s.add(ProductClaim(claim_id=cid,product_id=pid,field_name=f,value=str(row[f]),source_field=f,derivation_rule="原始CSV字段直接映射",confidence=1.0,verification_status="待复核"))
            derived={
                "demo_description":(f"{row['category']}商品：{row['title']}；现有功能标签为{row.get('feature_tags','未提供')}。","title|category|feature_tags","字段模板拼接",.95),
                "selling_point_candidates":(row.get("feature_tags", ""),"feature_tags","功能标签逐项作为候选卖点，不代表功效已验证",.8),
                "usage_scenario_candidate":(f"需要使用{row['title']}的场景","title","由商品名称推导的宽泛场景，非平台事实",.5),
            }
            for f,(value,source,rule,confidence) in derived.items():
                cid="CL-"+hashlib.sha256(f"{pid}|{f}|{value}".encode()).hexdigest()[:16]
                if value and not s.get(ProductClaim,cid): s.add(ProductClaim(claim_id=cid,product_id=pid,field_name=f,value=value,source_field=source,derivation_rule=rule,confidence=confidence,verification_status="待复核"))
            for asset_type in ["主图","功能图","参数图","场景图"]:
                aid="AS-"+hashlib.sha256(f"{pid}|{asset_type}".encode()).hexdigest()[:16]
                if not s.get(ProductAsset,aid): s.add(ProductAsset(asset_id=aid,product_id=pid,asset_type=asset_type,status="待采集"))
        for row in _raw_csv(root/"pintan_market_trending_products.csv"):
            if not s.get(TrendSignal,row["trend_id"]): s.add(TrendSignal(trend_id=row["trend_id"],payload=json.dumps(row,ensure_ascii=False)))
        s.flush()
        for row in _raw_csv(root/"pintan_trend_search_results.csv"):
            if not s.get(SearchCandidate,row["search_id"]): s.add(SearchCandidate(search_id=row["search_id"],trend_id=row["trend_id"],product_id=row["source_supply_id"],payload=json.dumps(row,ensure_ascii=False)))
            if row.get("match_score") and not s.get(TrendMatch,row["search_id"]): s.add(TrendMatch(search_id=row["search_id"],payload=json.dumps(row,ensure_ascii=False)))
        for row in stable:
            if row.get("total_score") and not s.get(StableScore,row["stable_id"]): s.add(StableScore(product_id=row["stable_id"],payload=json.dumps(row,ensure_ascii=False)))
    from .collector_mapping import backfill_mappings
    backfill_mappings(root)
    return counts

def export_csvs(root:Path, writer) -> dict[str,int]:
    """显式导出才改CSV；先写备份，任一失败可人工恢复。"""
    result={}
    for filename,(kind,_) in BUSINESS.items():
        rows=read_business(root,filename)
        # 新数据库允许某一业务表暂时为空（例如只维护爆款推荐、尚未导入
        # 稳定产品）。空表导出时沿用旧文件表头，避免为了写一个合法空 CSV
        # 而重新引入任何演示记录。
        fields=None
        path=root/filename
        if not rows and path.exists():
            with path.open("r",encoding="utf-8-sig",newline="") as handle:
                fields=next(csv.reader(handle),None)
        writer(path,rows,fields); result[kind]=len(rows)
    return result

def product_bundle(root:Path,product_id:str,trend_id:str|None=None)->dict:
    with session_scope(root) as s:
        p=s.get(Product,product_id)
        if not p: raise ValueError("商品不存在")
        claims=list(s.scalars(select(ProductClaim).where(ProductClaim.product_id==product_id).order_by(ProductClaim.claim_id)))
        # 新映射确认后，生成仅使用最新一次已确认映射的事实；历史版本的
        # 证据记录仍保留在库中供追溯，避免PE/HDPE旧值同时进入新文案。
        reviewed=s.scalar(select(CollectionRun).join(OfferSnapshot,OfferSnapshot.run_id==CollectionRun.run_id)
            .where(CollectionRun.product_id==product_id,CollectionRun.status=='已人工复核')
            .order_by(CollectionRun.created_at.desc(),CollectionRun.run_id.desc()))
        if reviewed:
            from .collector_mapping import read_mapping
            mapped=read_mapping(s,reviewed.run_id)
            ids={c['claim_id'] for c in mapped['content_claims'] if c.get('claim_id')}
            if ids or json.loads(reviewed.normalized_json).get('mapping_version'):
                claims=[c for c in claims if c.claim_id in ids]
        elif s.scalar(select(CollectionRun.run_id).where(CollectionRun.product_id==product_id,
                CollectionRun.normalized_json.contains('collector-mapping-v2.1'))):
            raise ValueError('请先在采集页面人工确认商品资料，再生成内容')
        assets=list(s.scalars(select(ProductAsset).where(ProductAsset.product_id==product_id,
            ProductAsset.status!='复核排除').order_by(ProductAsset.asset_id)))
        trend=None
        if trend_id:
            row=s.get(BusinessRow,{"kind":"trend","row_id":trend_id}); trend=json.loads(row.payload) if row else None
        stable=s.get(BusinessRow,{'kind':'stable','row_id':product_id})
        stable_data=json.loads(stable.payload) if stable else {}
        return {"selection_context":{k:stable_data.get(k,'') for k in ('selected_specification','spec_price_pairs','total_score','score_status','scoring_input_source')},"product":{"product_id":p.product_id,"title":p.title,"category":p.category,"verification_status":p.verification_status},
            "claims":[{"claim_id":c.claim_id,"field_name":c.field_name,"value":c.value,"source_field":c.source_field,"derivation_rule":c.derivation_rule,"confidence":c.confidence,"verification_status":c.verification_status} for c in claims],
            "assets":[{"asset_id":a.asset_id,"asset_type":a.asset_type,"status":a.status,"source_url":a.source_url,"ocr_text":a.ocr_text,"authorization_status":a.authorization_status} for a in assets],"trend":trend}

def read_business(root:Path, filename:str)->list[dict[str,str]]:
    kind,_=BUSINESS[filename]
    with session_scope(root) as s: return [json.loads(x.payload) for x in s.scalars(select(BusinessRow).where(BusinessRow.kind==kind).order_by(BusinessRow.row_id))]
def write_business(root:Path,filename:str,rows:list[dict[str,Any]]):
    kind,key=BUSINESS[filename]
    with session_scope(root) as s:
        old={x.row_id:x for x in s.scalars(select(BusinessRow).where(BusinessRow.kind==kind))}
        ids=set()
        for row in rows:
            rid=str(row[key]); ids.add(rid); payload=json.dumps({k:(v if isinstance(v,(dict,list)) else str(v)) for k,v in row.items()},ensure_ascii=False,sort_keys=True)
            if rid in old: old[rid].payload=payload
            else: s.add(BusinessRow(kind=kind,row_id=rid,payload=payload))
        for rid,obj in old.items():
            if rid not in ids: s.delete(obj)
        model={"stable":StableScore,"trend":TrendSignal,"search":SearchCandidate}[kind]
        for row in rows:
            payload=json.dumps(row,ensure_ascii=False); obj=s.get(model,row[key])
            if kind=="stable" and row.get("total_score") in (None,''):
                if obj:s.delete(obj)
                continue
            if obj: obj.payload=payload
            elif kind=="stable": s.add(StableScore(product_id=row[key],payload=payload))
            elif kind=="trend": s.add(TrendSignal(trend_id=row[key],payload=payload))
            else: s.add(SearchCandidate(search_id=row[key],trend_id=row["trend_id"],product_id=row["source_supply_id"],payload=payload))
            if kind=="search" and row.get("match_score"):
                match=s.get(TrendMatch,row[key])
                if match: match.payload=payload
                else: s.add(TrendMatch(search_id=row[key],payload=payload))
