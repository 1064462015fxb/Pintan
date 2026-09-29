import tempfile
import time
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from pintan.collector_bridge import VERSION, install_collector_bridge
from pintan.database import Base, engine, read_business
from pintan.trend_discovery import parse_lower_bound, score_sources, score_substitute

LOCAL={"Origin":"http://127.0.0.1:8010","Content-Type":"application/json"}
EXT={"Origin":"chrome-extension://"+"c"*32}


class TrendDiscoveryTests(unittest.TestCase):
    def test_visible_number_lower_bounds(self):
        self.assertEqual(parse_lower_bound("已售100+件"),100)
        self.assertEqual(parse_lower_bound("1.2万+人付款"),12000)
        self.assertIsNone(parse_lower_bound("暂无"))

    def test_relative_score_merge_and_coverage(self):
        rows=[
            {"source_id":"a","platform":"taobao","product_key":"1","title":"抽屉收纳盒","title_key":"抽屉收纳盒","url":"https://item.taobao.com/item.htm?id=1","price_value":20,"sales_value":1000,"engagement_value":80,"rank":1,"cover_url":"https://img/a.jpg","is_ad":False,"hard_block":""},
            {"source_id":"b","platform":"jd","product_key":"2","title":"抽屉收纳盒","title_key":"抽屉收纳盒","url":"https://item.jd.com/2.html","price_value":22,"sales_value":800,"engagement_value":50,"rank":2,"cover_url":"https://img/b.jpg","is_ad":False,"hard_block":""},
            {"source_id":"c","platform":"taobao","product_key":"3","title":"普通收纳盒","title_key":"普通收纳盒","url":"https://item.taobao.com/item.htm?id=3","price_value":8,"sales_value":10,"engagement_value":None,"rank":20,"cover_url":"https://img/c.jpg","is_ad":False,"hard_block":""},
        ]
        result=score_sources(rows,{"platforms":["taobao","jd"],"trend_score_threshold":70,"evidence_coverage_threshold":60})
        merged=next(x for x in result if x["title"]=="抽屉收纳盒")
        self.assertEqual(merged["platform_count"],2)
        self.assertEqual(merged["cross_platform_score"],60)
        self.assertEqual(merged["status"],"晋级")
        weak=next(x for x in result if x["title"]=="普通收纳盒")
        self.assertEqual(weak["evidence_coverage"],75)
        self.assertNotEqual(weak["status"],"晋级")

    def test_substitute_hard_gates(self):
        ok=score_substitute(visual=88,feature=85,price=80,pain=65,supplier=75,trend_retail=100,landed_cost=60)
        self.assertTrue(ok["qualified"])
        low=score_substitute(visual=70,feature=100,price=100,pain=100,supplier=100,trend_retail=100,landed_cost=50)
        self.assertFalse(low["qualified"]);self.assertIn("主图相似度低于75",low["blockers"])

    def test_bridge_platform_sequence_and_publish(self):
        tmp=tempfile.TemporaryDirectory();root=Path(tmp.name);Base.metadata.create_all(engine(root))
        app=FastAPI();install_collector_bridge(app,root)
        with TestClient(app,base_url='http://127.0.0.1:8010') as client:
            code=client.post('/api/collector/bridge/pair-code',headers=LOCAL,json={}).json()['code']
            pair=client.post('/api/collector/bridge/pair',headers=EXT,json={'code':code,'version':VERSION,'name':'trend-test'}).json()
            with client.websocket_connect('/api/collector/bridge/ws',headers=EXT) as ws:
                ws.send_json({'type':'hello',**pair,'version':VERSION});self.assertEqual(ws.receive_json()['type'],'hello_ack')
                response=client.post('/api/trend-discovery/batches',headers=LOCAL,json={'category':'厨房收纳','filters':{'platforms':['taobao']}})
                self.assertEqual(response.status_code,202,response.text);batch_id=response.json()['batch_id']
                command=ws.receive_json();self.assertEqual(command['type'],'trend_search');self.assertEqual(command['platform'],'taobao')
                paused=client.post(f'/api/trend-discovery/batches/{batch_id}/pause',headers=LOCAL,json={})
                self.assertEqual(paused.status_code,200,paused.text);self.assertEqual(paused.json()['status'],'paused')
                resumed=client.post(f'/api/trend-discovery/batches/{batch_id}/resume',headers=LOCAL,json={})
                self.assertEqual(resumed.status_code,200,resumed.text)
                command=ws.receive_json();self.assertEqual(command['type'],'trend_search')
                ws.send_json({'type':'trend_search_snapshot','batch_id':batch_id,'lease':command['lease'],'platform':'taobao','snapshot':{
                    'platform':'taobao','page_url':'https://s.taobao.com/search?q=x','candidates':[
                        {'product_id':'11','url':'https://item.taobao.com/item.htm?id=11','title':'厨房抽屉收纳盒','price_text':'29.9','sales_text':'已售1万+件','engagement_text':'评价1000+','rank':1,'cover_url':'https://127.0.0.1/11.jpg'},
                        {'product_id':'12','url':'https://item.taobao.com/item.htm?id=12','title':'普通盒','price_text':'19.9','sales_text':'已售10件','engagement_text':'评价1','rank':20,'cover_url':'https://127.0.0.1/12.jpg'}]}})
                messages=[ws.receive_json(),ws.receive_json()]
                ack=next(x for x in messages if x.get('type')=='trend_search_snapshot_ack');self.assertTrue(ack['completed'])
                supply=next(x for x in messages if x.get('type')=='trend_supply_search')
                ws.send_json({'type':'trend_supply_snapshot','batch_id':batch_id,'job_id':supply['job_id'],'lease':supply['lease'],'snapshot':{
                    'page_url':'https://s.1688.com/selloffer/offer_search.htm','keyword':'厨房收纳','candidates':[
                        {'page_url':'https://detail.1688.com/offer/998877.html','title':'厨房抽屉收纳盒','price':8.8,
                         'sales_text':'成交1000+','moq_text':'1件起批','is_ad':False,'relevance_rank':1,
                         'discovery_method':'anchor','cover_url':'https://127.0.0.1/c.jpg'}]}})
                supply_ack=ws.receive_json();self.assertTrue(supply_ack['completed'])
                view=client.get('/api/trend-discovery/batches/'+batch_id).json();self.assertIn(view['status'],{'supply_search','完成','部分完成','completed','partial'})
                self.assertEqual(len([x for x in view['candidates'] if x['status']=='晋级']),1)
                self.assertEqual(len(read_business(root,'pintan_market_trending_products.csv')),1)
        tmp.cleanup()

    def test_last_platform_failure_finishes_instead_of_stalling(self):
        tmp=tempfile.TemporaryDirectory();root=Path(tmp.name);Base.metadata.create_all(engine(root))
        app=FastAPI();install_collector_bridge(app,root)
        with TestClient(app,base_url='http://127.0.0.1:8010') as client:
            code=client.post('/api/collector/bridge/pair-code',headers=LOCAL,json={}).json()['code']
            pair=client.post('/api/collector/bridge/pair',headers=EXT,json={'code':code,'version':VERSION,'name':'trend-failure-test'}).json()
            with client.websocket_connect('/api/collector/bridge/ws',headers=EXT) as ws:
                ws.send_json({'type':'hello',**pair,'version':VERSION});ws.receive_json()
                batch_id=client.post('/api/trend-discovery/batches',headers=LOCAL,
                    json={'category':'厨房收纳','filters':{'platforms':['taobao']}}).json()['batch_id']
                command=ws.receive_json()
                ws.send_json({'type':'trend_search_progress','batch_id':batch_id,'lease':command['lease'],
                    'platform':'taobao','stage':'failed','error_type':'page_changed'})
                self.assertEqual(ws.receive_json()['type'],'trend_search_progress_ack')
                status='collecting'
                for _ in range(20):
                    status=client.get('/api/trend-discovery/batches/'+batch_id).json()['status']
                    if status not in {'queued','collecting'}:break
                    time.sleep(.01)
                self.assertIn(status,{'partial','部分完成'})
        tmp.cleanup()


if __name__=='__main__':unittest.main()
