"""真实WebSocket协议的离线集成测试；不将模拟助手冒充真实1688验收。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from pintan.collector_bridge import CollectorBridge, VERSION, install_collector_bridge
from pintan.database import Base, CollectionRun, CollectorHelper, CollectorTask, ProductAsset, engine, session_scope

LOCAL={"Origin":"http://127.0.0.1:8010", "Content-Type":"application/json"}
EXT={"Origin":"chrome-extension://"+"a"*32}
URL="https://detail.1688.com/offer/736208388272.html"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        Base.metadata.create_all(engine(self.root))
        app=FastAPI();install_collector_bridge(app,self.root)
        self.bridge=app.state.collector_bridge;self.bridge.model_enabled=False
        self.client=TestClient(app,base_url="http://127.0.0.1:8010")
        self.client.__enter__()
        self.sockets=[]
        self.pair=self.pair_helper()

    def tearDown(self):
        for context,ws in reversed(self.sockets):
            ws.close();context.__exit__(None,None,None)
        self.client.__exit__(None,None,None)
        self.tmp.cleanup()

    def post(self,url,data=None):
        return self.client.post(url,headers=LOCAL,json=data or {})

    def pair_helper(self,origin=EXT):
        code=self.post('/api/collector/bridge/pair-code').json()['code']
        response=self.client.post('/api/collector/bridge/pair',headers=origin,json={'code':code,'version':VERSION,'name':'离线测试助手'})
        self.assertEqual(response.status_code,200)
        return response.json()

    def socket(self,pair=None,origin=EXT):
        context=self.client.websocket_connect('/api/collector/bridge/ws',headers=origin)
        ws=context.__enter__();self.sockets.append((context,ws))
        ws.send_json(dict(type='hello',**(pair or self.pair),version=VERSION))
        self.assertEqual(ws.receive_json()['type'],'hello_ack')
        return ws

    def submit(self,ws,url=URL):
        response=self.post('/api/collector/tasks',{'page_url':url})
        self.assertEqual(response.status_code,202,response.text)
        command=ws.receive_json();self.assertEqual(command['type'],'collect')
        return command

    def test_existing_v2_helper_can_reconnect_without_pairing_again(self):
        context=self.client.websocket_connect('/api/collector/bridge/ws',headers=EXT)
        ws=context.__enter__();self.sockets.append((context,ws))
        ws.send_json(dict(type='hello',**self.pair,version='2.0.0'))
        self.assertEqual(ws.receive_json()['type'],'hello_ack')
        state=self.client.get('/api/collector/bridge').json()
        self.assertEqual(state['status'],'connected')
        self.assertEqual(state['helpers'][0]['version'],'2.0.0')

    def snapshot(self,command,images=None):
        return dict(type='snapshot',task_id=command['task_id'],lease=command['lease'],snapshot=dict(
            page_url=URL,title='车载垃圾袋',visible_text='材质：PE\n1包起批\n￥0.65-1.70',
            images=images or [],regions=[dict(region=r,recognized=True,complete=True,found=1,reason='fixture') for r in ['主图','SKU图','详情图']]))

    def wait(self,task_id):
        for _ in range(300):
            result=self.client.get('/api/collector/tasks/'+task_id).json()
            if result['status'] in {'completed','partial','failed','interrupted','cancelled'}:
                return result
            time.sleep(.01)
        self.fail('离线任务未在限定时间内完成')

    def test_offline_has_no_task_or_empty_run(self):
        r=self.post('/api/collector/tasks',{'page_url':URL})
        self.assertEqual(r.status_code,409)
        with session_scope(self.root) as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(CollectionRun)),0)

    def test_url_validation_and_local_csrf(self):
        for url in ['https://detail.1688.com.evil/offer/1.html','https://detail.1688.com/offer/a.html','javascript:alert(1)','https://u:p@detail.1688.com/offer/1.html','https://detail.1688.com:bad/offer/1.html']:
            self.assertEqual(self.post('/api/collector/tasks',{'page_url':url}).status_code,422)
        self.assertEqual(self.client.post('/api/collector/tasks',json={'page_url':URL}).status_code,403)
        self.assertEqual(self.client.post('/api/collector/tasks',headers={**LOCAL,'Origin':'https://evil.test'},json={'page_url':URL}).status_code,403)
        self.assertEqual(self.post('/api/collector/tasks',{'page_url':URL,'title':'不应要求标题'}).status_code,422)

    def test_pairing_expiry_single_use_and_hash_only(self):
        code=self.post('/api/collector/bridge/pair-code').json()['code']
        self.bridge.pair_until=0
        self.assertEqual(self.client.post('/api/collector/bridge/pair',headers=EXT,json={'code':code,'version':VERSION}).status_code,403)
        with session_scope(self.root) as s:
            h=s.get(CollectorHelper,self.pair['helper_id'])
            self.assertNotEqual(h.token_hash,self.pair['token'])
            self.assertEqual(len(h.token_hash),64)
        self.assertNotIn(self.pair['token'],self.client.get('/api/collector/bridge').text)

    def test_dedup_and_single_helper_queue(self):
        ws=self.socket();first=self.submit(ws)
        again=self.post('/api/collector/tasks',{'page_url':URL+'?tracking=1'}).json()
        self.assertTrue(again['deduplicated']);self.assertEqual(again['task_id'],first['task_id'])
        second=self.post('/api/collector/tasks',{'page_url':'https://detail.1688.com/offer/2.html'}).json()
        self.assertEqual(self.client.get('/api/collector/tasks/'+second['task_id']).json()['status'],'queued')
        self.post('/api/collector/tasks/'+first['task_id']+'/cancel')
        self.assertEqual(ws.receive_json()['type'],'cancel')
        self.assertEqual(ws.receive_json()['task_id'],second['task_id'])

    def test_pause_resume_lease_and_stale_snapshot(self):
        ws=self.socket();command=self.submit(ws)
        ws.send_json(dict(type='progress',task_id=command['task_id'],lease=command['lease'],stage='waiting_user',error_type='verification_required'))
        self.assertEqual(ws.receive_json()['type'],'progress_ack')
        state=self.client.get('/api/collector/tasks/'+command['task_id']).json()
        self.assertEqual(state['status'],'waiting_user')
        self.assertEqual(self.post('/api/collector/tasks/'+command['task_id']+'/resume').status_code,200)
        next_command=ws.receive_json();self.assertNotEqual(command['lease'],next_command['lease'])
        ws.send_json(self.snapshot(command));self.assertEqual(ws.receive_json()['type'],'rejected')

    def test_verification_waiting_task_is_automatically_retried_on_heartbeat(self):
        ws=self.socket();command=self.submit(ws)
        ws.send_json(dict(type='progress',task_id=command['task_id'],lease=command['lease'],
                          stage='waiting_user',error_type='verification_required'))
        self.assertEqual(ws.receive_json()['type'],'progress_ack')
        with session_scope(self.root) as s:
            task=s.get(CollectorTask,command['task_id']);payload=json.loads(task.payload)
            payload['auto_retry_at']=0;task.payload=json.dumps(payload)
        ws.send_json({'type':'heartbeat'})
        replies=[ws.receive_json(),ws.receive_json()]
        retry=next(x for x in replies if x['type']=='collect')
        self.assertNotEqual(retry['lease'],command['lease'])
        self.assertIn('heartbeat',{x['type'] for x in replies})

    def test_snapshot_product_mismatch_and_extra_credentials(self):
        ws=self.socket();command=self.submit(ws)
        bad=self.snapshot(command);bad['snapshot']['page_url']='https://detail.1688.com/offer/2.html'
        ws.send_json(bad);self.assertEqual(ws.receive_json()['type'],'rejected')
        bad=self.snapshot(command);bad['snapshot']['cookies']='not-a-real-cookie'
        ws.send_json(bad);self.assertEqual(ws.receive_json()['type'],'rejected')
        self.assertEqual(self.client.get('/api/collector/tasks/'+command['task_id']).json()['status'],'opening')

    def test_wrong_owner_cannot_progress_cancel_or_submit(self):
        ws=self.socket();command=self.submit(ws)
        other_origin={'Origin':'chrome-extension://'+'b'*32}
        other=self.socket(self.pair_helper(other_origin),other_origin)
        other.send_json(self.snapshot(command));self.assertEqual(other.receive_json()['type'],'rejected')
        other.send_json(dict(type='progress',task_id=command['task_id'],lease=command['lease'],stage='reading'))
        self.assertEqual(other.receive_json()['type'],'rejected')

    def test_duplicate_snapshot_is_idempotent_zero_images_partial(self):
        ws=self.socket();command=self.submit(ws);message=self.snapshot(command)
        ws.send_json(message);self.assertFalse(ws.receive_json()['duplicate'])
        result=self.wait(command['task_id']);self.assertEqual(result['status'],'partial')
        self.assertEqual(result['downloaded'],0)
        ws.send_json(message);self.assertTrue(ws.receive_json()['duplicate'])
        with session_scope(self.root) as s:self.assertEqual(s.scalar(select(func.count()).select_from(CollectionRun)),1)
        response=self.client.get('/api/collector/tasks/'+command['task_id']).text
        self.assertNotIn('snapshot_hash',response);self.assertNotIn(command['lease'],response)

    def test_empty_facts_with_images_fails(self):
        ws=self.socket();command=self.submit(ws);message=self.snapshot(command)
        message['snapshot'].update(title='',visible_text='',images=[dict(url='https://cbu01.alicdn.com/a.jpg',region='主图')])
        ws.send_json(message);self.assertTrue(ws.receive_json()['failed'])
        self.assertEqual(self.wait(command['task_id'])['status'],'failed')

    def test_per_image_failure_does_not_stop_other_download(self):
        calls=[]
        def download(root,aid):
            calls.append(aid)
            return {'asset_id':aid,'attempts':[{'status':'success' if len(calls)==2 else 'failed','error_type':'OfflineFixture'}]}
        with patch('pintan.collector_bridge.download_asset',side_effect=download):
            ws=self.socket();command=self.submit(ws)
            ws.send_json(self.snapshot(command,[dict(url='https://cbu01.alicdn.com/'+x+'.jpg',region='主图') for x in ['a','b']]))
            ws.receive_json();result=self.wait(command['task_id'])
        self.assertEqual(len(calls),2);self.assertEqual(result['downloaded'],1);self.assertEqual(result['failed'],1)
        self.assertEqual(result['status'],'partial')

    def test_model_failure_keeps_rules_and_images(self):
        self.bridge.model_enabled=True
        with patch.object(self.bridge,'normalize',side_effect=RuntimeError('safe fixture')):
            ws=self.socket();command=self.submit(ws);ws.send_json(self.snapshot(command));ws.receive_json()
            result=self.wait(command['task_id'])
        self.assertEqual(result['model_status'],'fallback')
        self.assertEqual(result['status'],'partial')
        self.assertTrue(result['run_id'])

    def test_missing_regions_partial_even_when_image_succeeds(self):
        with patch('pintan.collector_bridge.download_asset',return_value={'attempts':[{'status':'success'}]}):
            ws=self.socket();command=self.submit(ws)
            message=self.snapshot(command,[dict(url='https://cbu01.alicdn.com/a.jpg',region='主图')]);message['snapshot']['regions']=[]
            ws.send_json(message);ws.receive_json();result=self.wait(command['task_id'])
        self.assertEqual(result['status'],'partial')

    def test_completed_requires_all_regions_and_downloads(self):
        with patch('pintan.collector_bridge.download_asset',return_value={'attempts':[{'status':'success'}]}):
            ws=self.socket();command=self.submit(ws)
            ws.send_json(self.snapshot(command,[dict(url=f'https://cbu01.alicdn.com/{i}.jpg',region=r) for i,r in enumerate(['主图','SKU图','详情图'])]))
            ws.receive_json();result=self.wait(command['task_id'])
        self.assertEqual(result['status'],'completed')

    def test_disconnect_marks_interrupted_and_restart_keeps_progress(self):
        ws=self.socket();command=self.submit(ws);ws.close()
        result=self.wait(command['task_id']);self.assertEqual(result['status'],'interrupted')
        bridge2=CollectorBridge(self.root)
        self.assertEqual(bridge2.read(command['task_id'])['status'],'interrupted')
        self.assertEqual(bridge2.bridge_state()['status'],'offline')

    def test_server_restart_marks_pending_tasks(self):
        ws=self.socket();command=self.submit(ws)
        bridge2=CollectorBridge(self.root)
        self.assertEqual(bridge2.read(command['task_id'])['error_type'],'server_restarted')

    def test_unknown_task_and_cancelled_task_cannot_submit(self):
        self.assertEqual(self.client.get('/api/collector/tasks/missing').status_code,404)
        self.assertEqual(self.post('/api/collector/tasks/missing/cancel').status_code,404)
        ws=self.socket();command=self.submit(ws)
        self.post('/api/collector/tasks/'+command['task_id']+'/cancel');ws.receive_json()
        ws.send_json(self.snapshot(command));self.assertEqual(ws.receive_json()['type'],'rejected')


if __name__=='__main__':unittest.main()
