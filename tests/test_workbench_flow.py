"""Consumer workbench: no paid models, no production database writes."""
import io
import json
import zipfile
import asyncio
from unittest.mock import patch, MagicMock
from unittest.mock import AsyncMock
from PIL import Image
from sqlalchemy import select, func
from tests.test_content_readiness import ContentReadinessTests
from pintan.database import (session_scope, ProductCover, ProductAsset, AssetAnalysis,
    AngleDiagnosis, ContentChoice, ContentMaster, PlatformContent, PlatformContentVersion,
    GeneratedMedia, ListingPackage, MediaTask, CollectorHelper, utcnow)
from pintan.collector_bridge import VERSION
from pintan.workbench import capture_cover


class WorkbenchTests(ContentReadinessTests):
    def seed_content(self):
        self.client.post('/api/content/products/1688-123/confirm-facts', json={'run_id':self.run['run_id']})
        state=self.client.get('/api/content/products/1688-123/readiness').json()
        ids=[a['asset_id'] for a in state['assets']['items']]
        response=self.client.post('/api/content/products/1688-123/authorize-assets', json={
            'confirmed':True,'statement':'我已确认拥有该商品当前所选图片的使用权','asset_ids':ids})
        self.assertEqual(response.status_code,200,response.text)
        stamp=utcnow()
        with session_scope(self.root) as s:
            asset=s.scalar(select(ProductAsset).where(ProductAsset.product_id=='1688-123'))
            s.add(AngleDiagnosis(diagnosis_id='D',product_id='1688-123',cache_key='D',result_json='{}',model='test',prompt_version='test',created_at=stamp));s.flush()
            s.add(ContentChoice(choice_id='C',diagnosis_id='D',selected_json='[]',custom_angle='',created_at=stamp))
            s.add(ContentMaster(master_id='M',product_id='1688-123',cache_key='M',status='草稿',current_version=1,created_at=stamp));s.flush()
            s.add(PlatformContent(content_id='P',master_id='M',choice_id='C',platform='淘宝',market='国内',cache_key='P',current_version=1,risk_level='低',created_at=stamp));s.flush()
            s.add(PlatformContentVersion(content_id='P',version=1,visible_json='{"商品标题":"桌边垃圾袋"}',issues_json='[]',changes_json='[]',status='草稿',model='test',created_at=stamp))
            folder=self.root/'data/media';folder.mkdir(parents=True)
            Image.new('RGB',(20,20),'white').save(folder/'output.png')
            s.add(GeneratedMedia(media_id='G',product_id='1688-123',content_id='P',media_type='image',slot='轮播图-1',version=1,status='success',local_path='data/media/output.png',sha256='test',metadata_json=json.dumps({'content_version':1,'generation_mode':'conservative','source_asset_ids':[asset.asset_id],'source_hashes':['abc']}),created_at=stamp))
            self.aid=asset.asset_id

    def test_confirm_partial_group_idempotent_and_immutable(self):
        self.seed_content()
        self.assertEqual(len(self.client.get('/api/distribution/products').json()['items']),1)
        preview=self.client.get('/api/distribution/products/1688-123').json()
        self.assertEqual(len(preview['items'][0]['media']),1)
        body={'content_id':'P','content_version':1,'media_ids':['G']}
        response=self.client.post('/api/distribution/packages',json=body)
        self.assertEqual(response.status_code,200,response.text)
        package=response.json()
        self.assertEqual(self.client.post('/api/distribution/packages',json=body).json(),package)
        with session_scope(self.root) as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(ListingPackage)),1)
            s.get(PlatformContent,'P').current_version=2
        old=self.client.get('/api/distribution/packages/'+package['package_id']).json()
        self.assertEqual(old['content_version'],1);self.assertTrue(old['has_newer_content'])
        self.assertEqual(self.client.post('/api/distribution/packages',json=body).status_code,409)

    def test_listing_package_export_and_safe_assist_gate(self):
        self.seed_content()
        package=self.client.post('/api/distribution/packages',json={
            'content_id':'P','content_version':1,'media_ids':['G']}).json()
        detail=self.client.get('/api/distribution/packages/'+package['package_id'])
        self.assertEqual(detail.status_code,200,detail.text)
        listing=detail.json()['listing']
        self.assertEqual(listing['platform'],'淘宝')
        self.assertTrue(listing['title'])
        self.assertIn('不会点击发布',listing['notice'])
        exported=self.client.get('/api/distribution/packages/'+package['package_id']+'/download')
        self.assertEqual(exported.status_code,200,exported.text)
        self.assertEqual(exported.headers['content-type'],'application/zip')
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            names=archive.namelist()
            self.assertIn('上架资料.json',names)
            self.assertIn('SKU.csv',names)
            self.assertTrue(any(x.startswith('images/') for x in names))
        # No connected 2.7 helper means the browser cannot be driven implicitly.
        response=self.client.post('/api/distribution/packages/'+package['package_id']+'/assist',json={})
        self.assertEqual(response.status_code,409,response.text)

    def test_taobao_assist_dispatch_and_result_never_publish(self):
        self.seed_content()
        package=self.client.post('/api/distribution/packages',json={
            'content_id':'P','content_version':1,'media_ids':['G']}).json()
        with session_scope(self.root) as s:
            s.add(CollectorHelper(helper_id='H',token_hash='x',extension_origin='chrome-extension://'+'a'*32,
                name='test',version=VERSION,connected=True,last_seen=utcnow()))
        bridge=self.client.app.state.collector_bridge;socket=AsyncMock();bridge.connections['H']=socket
        started=self.client.post('/api/distribution/packages/'+package['package_id']+'/assist',json={})
        self.assertEqual(started.status_code,202,started.text)
        command=socket.send_json.await_args.args[0]
        self.assertEqual(command['type'],'listing_fill')
        self.assertNotIn('publish',command)
        asyncio.run(bridge.message('H',{'type':'listing_result','task_id':command['task_id'],'lease':command['lease'],
            'filled':['商品标题'],'warnings':['没有点击发布'],'current_url':'https://item.upload.taobao.com/router/publish.htm'}))
        result=self.client.get('/api/distribution/tasks/'+command['task_id']).json()
        self.assertEqual(result['status'],'completed');self.assertEqual(result['filled'],['商品标题'])

    def test_reject_missing_auth_and_foreign_image(self):
        self.seed_content()
        with session_scope(self.root) as s:
            s.get(ProductAsset,self.aid).sha256='changed'
        body={'content_id':'P','content_version':1,'media_ids':['G']}
        self.assertEqual(self.client.post('/api/distribution/packages',json=body).status_code,409)
        body['media_ids']=['foreign']
        self.assertEqual(self.client.post('/api/distribution/packages',json=body).status_code,409)
        body['media_ids']=[]
        self.assertEqual(self.client.post('/api/distribution/packages',json=body).status_code,409)

    def test_one_cover_is_not_a_content_asset(self):
        with session_scope(self.root) as s:
            for a in s.scalars(select(ProductAsset).where(ProductAsset.product_id=='1688-123')):a.local_path=None
            before=s.scalar(select(func.count()).select_from(ProductAsset))
        buf=io.BytesIO();Image.new('RGB',(800,800),'red').save(buf,'PNG')
        response=MagicMock();response.headers={'content-type':'image/png'};response.iter_bytes.return_value=[buf.getvalue()]
        response.__enter__.return_value=response
        with patch('pintan.workbench._safe_public_url',side_effect=lambda u:u),patch('pintan.workbench.httpx.stream',return_value=response) as get:
            result=capture_cover(self.root,'1688-123',{'images':[{'region':'主图','url':'https://a.example/1.png'},{'region':'主图','url':'https://a.example/2.png'}]})
            self.assertEqual(result['cover_status'],'available');self.assertEqual(get.call_count,1)
            capture_cover(self.root,'1688-123');self.assertEqual(get.call_count,1)
        with session_scope(self.root) as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(ProductAsset)),before)
            self.assertEqual(s.scalar(select(func.count()).select_from(AssetAnalysis)),0)
            self.assertIsNotNone(s.get(ProductCover,'1688-123'))
        self.assertEqual(self.client.get(result['cover_url']).status_code,200)

    def test_persistent_true_progress(self):
        with session_scope(self.root) as s:
            s.add(MediaTask(task_id='T',kind='images',product_id='1688-123',input_hash='t',status='completed',result_json=json.dumps({'items':[{'status':'success'},{'status':'generation_failed'}]}),created_at=utcnow(),updated_at=utcnow()))
        data=self.client.get('/api/media/tasks/T').json()
        self.assertEqual((data['completed_slots'],data['total_slots'],data['success_count'],data['failed_count']),(2,2,1,1))

    def test_cancel_keeps_completed_counts(self):
        with session_scope(self.root) as s:
            s.add(MediaTask(task_id='cancel',kind='images',product_id='1688-123',input_hash='cancel',status='running',result_json=json.dumps({'progress':{'completed_slots':1,'total_slots':3,'success_count':1,'failed_count':0}}),created_at=utcnow(),updated_at=utcnow()))
        self.assertEqual(self.client.post('/api/media/tasks/cancel/cancel').status_code,200)
        data=self.client.get('/api/media/tasks/cancel').json()
        self.assertEqual(data['status'],'cancelled');self.assertEqual(data['completed_slots'],1)

    def test_cover_private_url_failure_is_nonblocking(self):
        with session_scope(self.root) as s:
            for a in s.scalars(select(ProductAsset).where(ProductAsset.product_id=='1688-123')):a.local_path=None
        with patch('pintan.workbench.httpx.stream') as get:
            result=capture_cover(self.root,'1688-123',{'images':[{'region':'主图','url':'http://127.0.0.1/private'}]})
        self.assertEqual(result['cover_status'],'failed');get.assert_not_called()
