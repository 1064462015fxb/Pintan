import tempfile
import unittest
import json
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote_to_bytes, urlsplit

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from pintan.batch_selection import default_advice, detail_filter, filter_candidates
from pintan.collector_bridge import VERSION, install_collector_bridge
from pintan.database import Base, BatchSelectionItem, CollectorTask, ProductAsset, engine, session_scope

LOCAL={"Origin":"http://127.0.0.1:8010","Content-Type":"application/json"}
EXT={"Origin":"chrome-extension://"+"b"*32}


class BatchSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        Base.metadata.create_all(engine(self.root));app=FastAPI();install_collector_bridge(app,self.root)
        self.client=TestClient(app,base_url='http://127.0.0.1:8010');self.client.__enter__()
        code=self.client.post('/api/collector/bridge/pair-code',headers=LOCAL,json={}).json()['code']
        self.pair=self.client.post('/api/collector/bridge/pair',headers=EXT,
            json={'code':code,'version':VERSION,'name':'batch-test'}).json()
        self.context=self.client.websocket_connect('/api/collector/bridge/ws',headers=EXT);self.ws=self.context.__enter__()
        self.ws.send_json({'type':'hello',**self.pair,'version':VERSION});self.assertEqual(self.ws.receive_json()['type'],'hello_ack')

    def tearDown(self):
        self.ws.close();self.context.__exit__(None,None,None);self.client.__exit__(None,None,None);self.tmp.cleanup()

    def test_typo_advice_and_candidate_filter(self):
        advice=default_advice('家居归纳');self.assertEqual(advice['normalized_keyword'],'家居收纳')
        rows=[{'page_url':'https://detail.1688.com/offer/1.html','title':'收纳盒','relevance_rank':1},
              {'page_url':'https://detail.1688.com/offer/1.html','title':'重复','relevance_rank':2},
              {'page_url':'https://detail.1688.com/offer/2.html','title':'加盟教程','relevance_rank':3},
              {'page_url':'https://evil.test/offer/3.html','title':'危险','relevance_rank':4}]
        result=filter_candidates(rows,advice,60);self.assertEqual([x['offer_id'] for x in result],['1'])

    def test_text_snapshot_auto_enrolls_without_downloading(self):
        from pintan.collector import auto_enroll_run,collect_snapshot
        from pintan.database import read_business
        snap={'page_url':'https://detail.1688.com/offer/99.html','title':'桌面收纳盒',
            'text_blocks':[{'region':'title','text':'桌面收纳盒','source_locator':'title'},
              {'region':'attributes','text':'类目：收纳盒\n材质：PP\n功能：桌面收纳','source_locator':'attr'},
              {'region':'price','text':'1件起批','source_locator':'price'},
              {'region':'sku','text':'颜色\n白色小号\n¥3.50\n库存100件','source_locator':'sku'}],
            'skus':[{'text':'白色小号','price_cny':3.5,'source_locator':'sku'}],
            'images':[{'url':'https://cbu01.alicdn.com/a.jpg','region':'SKU图','sku':'白色小号'}]}
        run=collect_snapshot(self.root,snap,False);auto_enroll_run(self.root,run['run_id'])
        rows=read_business(self.root,'pintan_stable_products.csv');self.assertEqual(rows,[])
        from pintan.database import BusinessRow
        with session_scope(self.root) as session:
            candidate=session.get(BusinessRow,('candidate',run['product_id']))
            self.assertIsNotNone(candidate)
            self.assertEqual(json.loads(candidate.payload)['verification_status'],'批量候选·待评分')
        with session_scope(self.root) as session:
            asset=session.scalar(select(ProductAsset));self.assertIsNone(asset.local_path);self.assertEqual(asset.status,'待下载')

    def test_same_offer_with_tracking_urls_updates_one_candidate(self):
        from pintan.collector import auto_enroll_run,collect_snapshot
        from pintan.database import BusinessRow,CollectionRun,Product
        base={'title':'厨房收纳架','text_blocks':[{'region':'title','text':'厨房收纳架','source_locator':'title'},
            {'region':'attributes','text':'类目：收纳架\n材质：PP','source_locator':'attr'},
            {'region':'price','text':'1件起批','source_locator':'price'},
            {'region':'sku','text':'颜色\n白色\n¥9.90\n库存100件','source_locator':'sku'}],
            'skus':[{'text':'白色','price_cny':9.9,'source_locator':'sku'}],'images':[]}
        first=collect_snapshot(self.root,{**base,'page_url':'https://detail.1688.com/offer/7788.html?spm=one#detail'},False)
        second=collect_snapshot(self.root,{**base,'page_url':'https://detail.1688.com/offer/7788.html?from=search'},False)
        auto_enroll_run(self.root,first['run_id']);auto_enroll_run(self.root,second['run_id'])
        with session_scope(self.root) as s:
            self.assertEqual(len(list(s.scalars(select(Product)))),1)
            self.assertEqual(len(list(s.scalars(select(CollectionRun)))),2)
            candidates=list(s.scalars(select(BusinessRow).where(BusinessRow.kind=='candidate')))
            self.assertEqual(len(candidates),1)
            data=json.loads(candidates[0].payload)
            self.assertEqual(data['product_url'],'https://detail.1688.com/offer/7788.html')

    def test_detail_filter_keeps_unknown_fields_but_rejects_explicit_mismatch(self):
        from pintan.collector import collect_snapshot
        unknown={'page_url':'https://detail.1688.com/offer/991.html','title':'厨房收纳盒',
            'text_blocks':[{'region':'title','text':'厨房收纳盒','source_locator':'title'},
                {'region':'sku','text':'白色 ¥3.50','source_locator':'sku'}],
            'skus':[{'text':'白色','price_cny':3.5,'source_locator':'sku'}],'images':[]}
        run=collect_snapshot(self.root,unknown,False)
        accepted,reason=detail_filter(self.root,run['run_id'],{
            'max_moq':5,'min_sales':50,'require_dropship':True,
            'require_free_shipping':True,'min_supplier_years':2,'min_return_rate_pct':20})
        self.assertTrue(accepted,reason)
        explicit={**unknown,'page_url':'https://detail.1688.com/offer/992.html','visible_text':'不支持一件代发',
            'text_blocks':unknown['text_blocks']+[{'region':'service','text':'不支持一件代发','source_locator':'service'}]}
        rejected=collect_snapshot(self.root,explicit,False)
        self.assertEqual(detail_filter(self.root,rejected['run_id'],{'require_dropship':True}),
                         (False,'页面明确不支持一件代发'))

    def test_search_creates_text_only_tasks_without_download(self):
        response=self.client.post('/api/batch-selection',headers=LOCAL,json={
            'original_keyword':'家居归纳','normalized_keyword':'家居收纳','filters':{'exclude_ads':True},
            'target_count':2,'max_candidates':3})
        self.assertEqual(response.status_code,202,response.text);batch_id=response.json()['batch_id']
        command=self.ws.receive_json();self.assertEqual(command['type'],'search')
        encoded=urlsplit(command['page_url']).query.split('keywords=',1)[1]
        self.assertEqual(unquote_to_bytes(encoded).decode('gb18030'),'家居收纳')
        candidates=[{'page_url':f'https://detail.1688.com/offer/{i}.html','title':f'收纳商品{i}',
            'relevance_rank':i,'price':float(i),'sales_text':'已售100+件','moq_text':'1件起批','is_ad':False} for i in range(1,4)]
        self.ws.send_json({'type':'search_snapshot','batch_id':batch_id,'lease':command['lease'],
            'snapshot':{'page_url':'https://s.1688.com/selloffer/offer_search.htm','keyword':'家居收纳',
                        'candidates':candidates,'diagnostics':[]}})
        first=self.ws.receive_json();second=self.ws.receive_json()
        messages={x['type']:x for x in (first,second)};ack=messages['search_snapshot_ack'];detail=messages['collect']
        self.assertEqual(ack['found'],3);self.assertEqual(detail['mode'],'text_only')
        with session_scope(self.root) as session:
            tasks=list(session.scalars(select(CollectorTask)));self.assertEqual(len(tasks),3)
            self.assertTrue(all(__import__('json').loads(x.payload)['mode']=='text_only' for x in tasks))
            self.assertEqual(len(list(session.scalars(select(ProductAsset)))),0)
            self.assertEqual(len(list(session.scalars(select(BatchSelectionItem)))),3)

    def test_batch_requires_latest_extension(self):
        self.ws.close();self.context.__exit__(None,None,None)
        # Reconnect the same helper as an old, single-product-compatible client.
        self.context=self.client.websocket_connect('/api/collector/bridge/ws',headers=EXT);self.ws=self.context.__enter__()
        self.ws.send_json({'type':'hello',**self.pair,'version':'2.3.2'});self.assertEqual(self.ws.receive_json()['type'],'hello_ack')
        response=self.client.post('/api/batch-selection',headers=LOCAL,json={
            'original_keyword':'收纳','normalized_keyword':'收纳','filters':{}})
        self.assertEqual(response.status_code,409)

    def test_empty_search_retry_clears_snapshot_fingerprint(self):
        response=self.client.post('/api/batch-selection',headers=LOCAL,json={
            'original_keyword':'厨房用品','normalized_keyword':'厨房用品','filters':{},
            'target_count':2,'max_candidates':3})
        batch_id=response.json()['batch_id'];command=self.ws.receive_json()
        empty={'page_url':'https://s.1688.com/selloffer/offer_search.htm','keyword':'厨房用品',
               'candidates':[],'diagnostics':['详情链接锚点 0 个','去重候选 0 个']}
        self.ws.send_json({'type':'search_snapshot','batch_id':batch_id,'lease':command['lease'],'snapshot':empty})
        self.assertEqual(self.ws.receive_json()['type'],'search_snapshot_ack')
        self.assertEqual(self.client.get(f'/api/batch-selection/{batch_id}').json()['status'],'failed')
        resumed=self.client.post(f'/api/batch-selection/{batch_id}/resume',headers=LOCAL,json={})
        self.assertEqual(resumed.status_code,200,resumed.text);retry=self.ws.receive_json()
        self.assertEqual(retry['type'],'search')
        with session_scope(self.root) as session:
            from pintan.database import BatchSelection
            payload=json.loads(session.get(BatchSelection,batch_id).payload)
            self.assertNotIn('snapshot_hash',payload)
        self.ws.send_json({'type':'search_snapshot','batch_id':batch_id,'lease':retry['lease'],'snapshot':empty})
        self.assertEqual(self.ws.receive_json()['type'],'search_snapshot_ack')
        self.assertEqual(self.client.get(f'/api/batch-selection/{batch_id}').json()['status'],'failed')

    def test_text_detail_timeout_skips_to_next_candidate(self):
        response=self.client.post('/api/batch-selection',headers=LOCAL,json={
            'original_keyword':'厨房用品','normalized_keyword':'厨房用品','filters':{},
            'target_count':2,'max_candidates':2})
        batch_id=response.json()['batch_id'];search=self.ws.receive_json()
        candidates=[{'page_url':f'https://detail.1688.com/offer/{offer}.html','title':f'厨房商品{offer}',
            'relevance_rank':rank,'is_ad':False} for rank,offer in enumerate((101001,101002),1)]
        self.ws.send_json({'type':'search_snapshot','batch_id':batch_id,'lease':search['lease'],
            'snapshot':{'page_url':'https://s.1688.com/selloffer/offer_search.htm','keyword':'厨房用品',
                        'candidates':candidates,'diagnostics':[]}})
        messages=[self.ws.receive_json(),self.ws.receive_json()]
        first=next(x for x in messages if x['type']=='collect')
        with session_scope(self.root) as session:
            task=session.get(CollectorTask,first['task_id']);task.status='reading'
            payload=json.loads(task.payload);payload['dispatched_at']=time.time()-301;task.payload=json.dumps(payload)
        self.ws.send_json({'type':'heartbeat'})
        replies=[self.ws.receive_json(),self.ws.receive_json()]
        second=next(x for x in replies if x['type']=='collect')
        self.assertNotEqual(first['task_id'],second['task_id'])
        with session_scope(self.root) as session:
            rows=list(session.scalars(select(BatchSelectionItem).where(BatchSelectionItem.batch_id==batch_id)
                .order_by(BatchSelectionItem.relevance_rank)))
            self.assertEqual(rows[0].status,'failed');self.assertEqual(rows[1].status,'collecting')


if __name__=='__main__':unittest.main()
