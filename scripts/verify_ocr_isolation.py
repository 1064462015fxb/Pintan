"""Read-only real OCR smoke test while timing the running workstation API."""
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def main():
    from sqlalchemy import select
    from pintan.database import ProductAsset,session_scope
    from pintan.media_pipeline import _ocr
    from pintan.ocr_worker import worker
    with session_scope(ROOT) as session:
        row=session.scalar(select(ProductAsset).where(
            ProductAsset.product_id=='1688-953151089997',
            ProductAsset.local_path.is_not(None),ProductAsset.width>=600).limit(1))
        if row is None:raise RuntimeError('No real source image found')
        asset_id=row.asset_id;path=ROOT/row.local_path
    start=time.monotonic();timings=[]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(_ocr,path.read_bytes(),strict=True)
            while not future.done():
                tick=time.monotonic()
                with urllib.request.urlopen('http://127.0.0.1:8010/api/media/products/1688-953151089997',timeout=5) as response:
                    response.read()
                timings.append(round((time.monotonic()-tick)*1000,1))
                time.sleep(.5)
            try:
                text=future.result()
                result={'ocr_status':'success','recognized_characters':len(text)}
            except Exception as exc:
                result={'ocr_status':'failed','error_type':type(exc).__name__,'reason':str(exc)}
        result.update(asset_id=asset_id,seconds=round(time.monotonic()-start,1),
            api_requests=len(timings),api_max_ms=max(timings,default=0),
            api_average_ms=round(sum(timings)/max(1,len(timings)),1),
            paddle_loaded_in_parent='paddle' in sys.modules)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['ocr_status']=='success' else 1
    finally:worker.close()


if __name__=='__main__':raise SystemExit(main())
