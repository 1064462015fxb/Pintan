"""品探命令行：初始化、校验、两条独立流水线及本地调试服务。"""
from __future__ import annotations
import argparse, json, logging
from pathlib import Path
from dotenv import load_dotenv
from .agents import run_stable_agent, run_trending_agent
from .data_store import TREND_FILE, read_csv, validate_three_tables, atomic_write_csv_raw
from .database import clear_database_content, export_csvs, init_database, set_data_mode
from .demo_data import initialize_demo

def build_parser() -> argparse.ArgumentParser:
    parser=argparse.ArgumentParser(prog="pintan",description="品探双 Agent 原型"); parser.add_argument("--root",type=Path,default=Path.cwd())
    sub=parser.add_subparsers(dest="command",required=True); sub.add_parser("init-demo",help="初始化演示表和共享数据库"); sub.add_parser("clear-data",help="备份并清空数据库，切换为手工数据模式"); sub.add_parser("export-csv",help="从共享数据库显式导出三表"); sub.add_parser("validate",help="校验三张业务表")
    stable=sub.add_parser("stable",help="运行稳定评分 Agent"); stable.add_argument("--no-model",action="store_true")
    trending=sub.add_parser("trending",help="运行爆款匹配 Agent"); scope=trending.add_mutually_exclusive_group(required=True); scope.add_argument("--trend-id",action="append"); scope.add_argument("--all",action="store_true")
    trending.add_argument("--force", action="store_true", help="绕过成功缓存，强制重新匹配")
    serve=sub.add_parser("serve",help="启动本地调试页"); serve.add_argument("--port",type=int,default=8000); sub.add_parser("run",help="旧命令（已停用）"); return parser

def main(argv:list[str]|None=None)->int:
    args=build_parser().parse_args(argv); root=args.root.resolve(); load_dotenv(root/".env",override=False); logging.basicConfig(level=logging.INFO,format="%(levelname)s %(message)s")
    try:
        if args.command=="init-demo":
            if not (root/"pintan_stable_products.csv").exists(): initialize_demo(root)
            set_data_mode(root,"demo")
            result=init_database(root)
        elif args.command=="clear-data":
            backup=clear_database_content(root);result={"status":"cleared","mode":"manual","backup":str(backup)}
        elif args.command=="export-csv": result=export_csvs(root,atomic_write_csv_raw)
        elif args.command=="validate": result=validate_three_tables(root)
        elif args.command=="stable": result=run_stable_agent(root,use_model=not args.no_model)
        elif args.command=="trending":
            ids=[x["trend_id"] for x in read_csv(root/TREND_FILE)] if args.all else args.trend_id; result={i:run_trending_agent(root,i,force=args.force) for i in ids}
        elif args.command=="serve":
            import uvicorn
            from .webapp import create_app
            uvicorn.run(create_app(root),host="127.0.0.1",port=args.port); return 0
        else: raise ValueError("旧 run 命令已停用，请使用 stable 或 trending")
        print(json.dumps(result,ensure_ascii=False)); return 0
    except Exception as exc:
        unsafe=type(exc).__module__.startswith(("openai","httpx","langchain")); message="模型服务调用失败，请检查本地配置" if unsafe else str(exc)
        logging.getLogger("pintan").error("%s: %s",type(exc).__name__,message); return 1
