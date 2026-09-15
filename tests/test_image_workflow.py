"""分类语义、规划引用、失败隔离与OCR调用顺序的行为测试。"""
import json
import tempfile
import unittest
import hashlib
import threading
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from sqlalchemy import select
from pintan.database import Base,engine,session_scope,Product,ProductAsset,AssetAnalysis,AssetAuthorization,ImageReview,GeneratedMedia,ImagePlan
from pintan.image_workflow import (classify_product,analyses,validate_plan,validate_classification,
                                   generate_planned,prefer_clean_primary,audit_profile,copy_for_slot,
                                   render_copy,dimension_spec,render_dimension_board,parameter_board_prompt,
                                   normalize_generation_mode,normalize_slot_modes,resolve_generation_mode,
                                   scope_plan_to_available_materials,image_source_identity)

def classification():
    return dict(primary_type='使用场景图',tags=['产品正面图'],confidence=.9,
                reason='可见商品正面与汽车座椅',same_product=True,suitable=True,risks=[])

def review(*,score=95,blocking=None,warnings=None,correction=''):
    # 按40/25/20/15比例构造总分，便于测试不同用户阈值。
    return dict(product_consistency_score=score*.40,factual_compliance_score=score*.25,
                purpose_fulfilment_score=score*.20,visual_quality_score=score*.15,
                blocking_issues=blocking or [],warnings=warnings or [],suggestions=[],
                correction=correction,confidence=.9)

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        Base.metadata.create_all(engine(self.root))
        with session_scope(self.root) as s:
            s.add(Product(product_id='p',title='垃圾袋',category='日用品'));s.flush()
            for name,size,kind in [('small',(160,160),'SKU图'),('large',(800,800),'主图'),('tall',(750,900),'详情图')]:
                Image.new('RGB',size).save(self.root/f'{name}.png')
                s.add(ProductAsset(asset_id=name,product_id='p',asset_type=kind,status='待授权确认',local_path=f'{name}.png'))
    def tearDown(self):self.temp.cleanup()
    def test_all_types_ocr_then_classify_and_cache(self):
        calls=[]
        with patch('pintan.media_pipeline._ocr',side_effect=lambda b,**kw:calls.append('ocr') or '文字'),patch('pintan.image_workflow.visual_json',side_effect=lambda *a:calls.append('vision') or classification()):
            classify_product(self.root,'p');classify_product(self.root,'p')
        self.assertEqual(calls,['ocr','vision','ocr','vision'])
        self.assertEqual(analyses(self.root,'p')['small']['status'],'filtered')
        with session_scope(self.root) as s:self.assertEqual(s.get(ProductAsset,'large').asset_type,'主图')
    def test_single_failure_does_not_stop_others(self):
        with patch('pintan.media_pipeline._ocr',return_value=''),patch('pintan.image_workflow.visual_json',side_effect=[ValueError('bad'),classification()]):
            result=classify_product(self.root,'p')
        self.assertEqual(result['failed'],1)
        self.assertEqual(analyses(self.root,'p')['tall']['status'],'success')

    def test_ocr_failure_still_calls_visual_classifier(self):
        with patch('pintan.media_pipeline._ocr',side_effect=RuntimeError('ocr unavailable')),patch('pintan.image_workflow.visual_json',return_value=classification()) as model:
            result=classify_product(self.root,'p',asset_ids=['large','small'])
        self.assertEqual(model.call_count,1)
        self.assertEqual(result['failed'],0)
        self.assertEqual(analyses(self.root,'p')['large']['ocr_error'],'RuntimeError')

    def test_user_feedback_keeps_parent_and_original_inputs(self):
        raw=(self.root/'large.png').read_bytes();digest=hashlib.sha256(raw).hexdigest()
        slot=dict(section='轮播图',index=1,purpose='白底商品',source_asset_ids=['large'],prompt='白底')
        with session_scope(self.root) as s:
            s.add(AssetAuthorization(authorization_id='feedback-auth',asset_id='large',asset_sha256=digest,confirmed=True,statement='test',confirmed_at='now'))
            s.add(ImagePlan(plan_id='feedback-plan',content_id='feedback-content',input_hash='test',payload=json.dumps({'slots':[slot]}),created_at='now'))
            s.add(GeneratedMedia(media_id='parent',product_id='p',content_id='feedback-content',media_type='image',slot='轮播图-1',version=1,status='success',local_path='large.png',metadata_json=json.dumps({'plan_id':'feedback-plan'}),created_at='now'))
        with patch('pintan.media_pipeline._platform_record',return_value=(SimpleNamespace(platform='淘宝'),SimpleNamespace(product_id='p'),{},1)),patch('pintan.content_flow.scan',return_value=[]),patch('pintan.database.product_bundle',return_value={}),patch('pintan.image_workflow.generate_candidate',return_value=raw) as gen:
            output=generate_planned(self.root,'feedback-content',audit_level='none',user_feedback='减少留白',parent_media_id='parent')
        self.assertIn('减少留白',gen.call_args.args[0]);self.assertEqual(len(gen.call_args.args[1]),2)
        with session_scope(self.root) as s:
            meta=json.loads(s.get(GeneratedMedia,output['items'][0]['media_id']).metadata_json)
            self.assertEqual(meta['parent_media_id'],'parent');self.assertEqual(meta['user_feedback'],'减少留白')
            self.assertEqual(s.get(GeneratedMedia,'parent').version,1)
    def test_reject_unknown_class(self):
        value=classification();value['primary_type']='随便'
        with self.assertRaises(ValueError):validate_classification(value)
    def test_non_product_not_suitable(self):
        value=classification();value['same_product']=False
        self.assertFalse(validate_classification(value)['suitable'])
    def test_plan_refs_and_order(self):
        slot=dict(section='轮播图',index=1,purpose='展示正面',source_asset_ids=['large'],prompt='正面白底')
        validate_plan(dict(slots=[slot],gaps=[]),['large'])
        with self.assertRaises(ValueError):validate_plan(dict(slots=[slot,slot],gaps=[]),['large'])
        with self.assertRaises(ValueError):validate_plan(dict(slots=[slot],gaps=[]),['other'])
    def test_no_guessed_sku(self):
        slot=dict(section='SKU图',index=1,purpose='红色款',source_asset_ids=['large'],prompt='红色')
        with self.assertRaises(ValueError):validate_plan(dict(slots=[slot],gaps=[]),['large'])
    def test_plan_allows_text_only_slot_without_source(self):
        slot=dict(section='详情页',index=1,purpose='原创使用场景',source_asset_ids=[],
                  generation_strategy='text_only',prompt='根据文字事实策划')
        value=validate_plan(dict(slots=[slot],gaps=[]),['large'])
        self.assertEqual(value['slots'][0]['source_asset_ids'],[])
        slot['generation_strategy']='source_based'
        with self.assertRaises(ValueError):validate_plan(dict(slots=[slot],gaps=[]),['large'])
    def test_unavailable_sku_material_is_nonblocking_note(self):
        result=scope_plan_to_available_materials({'slots':[],'gaps':['缺少明确的SKU ID关联图片','缺少铝合金脚架的独立特写素材']})
        self.assertEqual(result['gaps'],[])
        self.assertEqual(len(result['omitted_notes']),2)
    def test_1688_thumbnail_and_high_resolution_share_identity(self):
        base='https://cbu01.alicdn.com/img/ibank/O1CN-test.jpg'
        self.assertEqual(image_source_identity(base+'_sum.jpg'),image_source_identity(base+'_.webp'))
        self.assertEqual(image_source_identity(base+'_b.jpg?x=1'),image_source_identity(base))
    def test_primary_prefers_clean_reference(self):
        plan={'slots':[dict(section='轮播图',index=1,purpose='正面',source_asset_ids=['dirty'],sku_id=None,prompt='主图')],'gaps':[]}
        cls=classification()
        pool={'dirty':{'classification':cls,'ocr_text':'限时包邮 无痕耐高温 车载垃圾袋'},
              'clean':{'classification':cls,'ocr_text':'Good luck'}}
        result=prefer_clean_primary(plan,pool)
        self.assertEqual(result['slots'][0]['source_asset_ids'],['clean'])
        self.assertTrue(result['automatic_adjustments'])

    def test_three_failed_reviews_keep_no_publishable_image(self):
        raw=(self.root/'large.png').read_bytes();digest=hashlib.sha256(raw).hexdigest()
        with session_scope(self.root) as s:
            s.add(AssetAuthorization(authorization_id='auth',asset_id='large',asset_sha256=digest,
                confirmed=True,statement='test',confirmed_at='now'))
        plan={'plan_id':'plan','slots':[dict(section='轮播图',index=1,purpose='主体',source_asset_ids=['large'],prompt='保留主体')]}
        with patch('pintan.media_pipeline._platform_record',return_value=(SimpleNamespace(platform='淘宝'),SimpleNamespace(product_id='p'),{},1)),patch('pintan.content_flow.scan',return_value=[]),patch('pintan.database.product_bundle',return_value={}),patch('pintan.image_workflow.plan_images',return_value=plan),patch('pintan.image_workflow.generate_candidate',return_value=raw) as gen,patch('pintan.media_pipeline._ocr',return_value='Good luck'),patch('pintan.image_workflow.visual_json',return_value=review(score=70,blocking=['印花改变'],correction='恢复印花')):
            result=generate_planned(self.root,'content',audit_level='strict')
        self.assertEqual(gen.call_count,3)
        self.assertIn('审核反馈包',gen.call_args_list[1].args[0])
        self.assertIn('印花改变',gen.call_args_list[1].args[0])
        self.assertEqual(len(gen.call_args_list[1].args[1]),2) # 原图 + 上轮成品修复目标
        self.assertEqual(result['items'][0]['status'],'review_failed')
        self.assertEqual(result['items'][0]['resolution']['code'],'product_inconsistency')
        with session_scope(self.root) as s:
            self.assertEqual(s.query(ImageReview).count(),3)
            self.assertIsNone(s.query(GeneratedMedia).one().local_path)

    def test_review_feedback_can_pass_second_attempt(self):
        raw=(self.root/'large.png').read_bytes();digest=hashlib.sha256(raw).hexdigest()
        with session_scope(self.root) as s:
            s.add(AssetAuthorization(authorization_id='auth2',asset_id='large',asset_sha256=digest,
                confirmed=True,statement='test',confirmed_at='now'))
        plan={'plan_id':'plan2','slots':[dict(section='轮播图',index=1,purpose='白底正面展示',source_asset_ids=['large'],prompt='白底正面')]}
        reviews=[review(score=70,blocking=['外围文字'],correction='删除外围文字'),review(score=82)]
        with patch('pintan.media_pipeline._platform_record',return_value=(SimpleNamespace(platform='淘宝'),SimpleNamespace(product_id='p'),{},1)),patch('pintan.content_flow.scan',return_value=[]),patch('pintan.database.product_bundle',return_value={}),patch('pintan.image_workflow.plan_images',return_value=plan),patch('pintan.image_workflow.generate_candidate',return_value=raw) as gen,patch('pintan.media_pipeline._ocr',return_value='Good luck'),patch('pintan.image_workflow.visual_json',side_effect=reviews):
            result=generate_planned(self.root,'content2')
        self.assertEqual(gen.call_count,2)
        self.assertEqual(len(gen.call_args_list[1].args[1]),2)
        self.assertEqual(result['items'][0]['status'],'success')

    def test_user_audit_thresholds_and_warning_policy(self):
        self.assertEqual(audit_profile('light')['threshold'],60)
        self.assertEqual(audit_profile('normal')['threshold'],75)
        self.assertEqual(audit_profile('strict')['threshold'],90)
        with self.assertRaises(ValueError):audit_profile('whatever')

    def test_copy_policy_keeps_search_cover_clean_and_traces_detail_copy(self):
        fields={'核心卖点':['可立可挂','按需取用'],'规格参数':['18CM×38CM','每包50只']}
        cover={'section':'轮播图','index':1,'purpose':'搜索首图'}
        detail={'section':'详情页','index':2,'purpose':'尺寸规格参数'}
        self.assertEqual(copy_for_slot(fields,cover),[])
        copies=copy_for_slot(fields,detail)
        self.assertTrue(copies)
        self.assertTrue(all(x['source_field']=='规格参数' for x in copies[:2]))
        self.assertIn('18CM×38CM',[x['text'] for x in copies])
        original=Image.new('RGB',(800,800),'white')
        rendered=render_copy(original,copies,detail)
        self.assertNotEqual(rendered.tobytes(),original.tobytes())

    def test_dimension_board_uses_verified_tuple(self):
        fields={'规格参数':['尺寸：25x18.5x5.5cm'],'核心卖点':['大容量']}
        spec=dimension_spec(fields)
        self.assertEqual((spec['width'],spec['height'],spec['depth'],spec['unit']),('25','18.5','5.5','cm'))
        original=Image.new('RGB',(750,1500),'white')
        rendered=render_dimension_board(original,spec)
        self.assertNotEqual(rendered.tobytes(),original.tobytes())
        self.assertIsNone(dimension_spec({'核心卖点':['大容量']}))
        prompt=parameter_board_prompt(spec)
        self.assertIn('25cm',prompt);self.assertIn('18.5cm',prompt);self.assertIn('5.5cm',prompt)
        self.assertNotIn('每个袋子都有标准',prompt)

    def test_no_rules_skips_ocr_and_visual_review(self):
        raw=(self.root/'large.png').read_bytes();digest=hashlib.sha256(raw).hexdigest()
        with session_scope(self.root) as s:
            s.add(AssetAuthorization(authorization_id='auth3',asset_id='large',asset_sha256=digest,
                confirmed=True,statement='test',confirmed_at='now'))
        plan={'plan_id':'plan3','slots':[dict(section='轮播图',index=1,purpose='主体',source_asset_ids=['large'],prompt='主体')]}
        with patch('pintan.media_pipeline._platform_record',return_value=(SimpleNamespace(platform='淘宝'),SimpleNamespace(product_id='p'),{},1)),patch('pintan.content_flow.scan',return_value=[]),patch('pintan.database.product_bundle',return_value={}),patch('pintan.image_workflow.plan_images',return_value=plan),patch('pintan.image_workflow.generate_candidate',return_value=raw),patch('pintan.media_pipeline._ocr') as ocr,patch('pintan.image_workflow.visual_json') as vision:
            result=generate_planned(self.root,'content3',audit_level='none')
        self.assertEqual(result['items'][0]['status'],'success')
        ocr.assert_not_called();vision.assert_not_called()

    def test_mode_priority_and_validation(self):
        normal=dict(section='轮播图',index=1,purpose='正面商品图',prompt='白底')
        concept=dict(section='详情页',index=1,purpose='厨房痛点场景',prompt='痛点引入')
        self.assertEqual(resolve_generation_mode(normal,'conservative',{'轮播图-1':'creative'}),('creative','slot_override'))
        self.assertEqual(resolve_generation_mode(normal,'creative',{}),('creative','global'))
        self.assertEqual(resolve_generation_mode(concept,'creative',{'详情页-1':'conservative'}),('text_only_concept','automatic'))
        self.assertEqual(normalize_generation_mode(None),'conservative')
        with self.assertRaises(ValueError):normalize_generation_mode('text_only')
        with self.assertRaises(ValueError):normalize_slot_modes({'不存在-1':'creative'},{'轮播图-1'})

    def test_conservative_sends_original_and_creative_sends_white_cutout(self):
        raw=(self.root/'large.png').read_bytes();digest=hashlib.sha256(raw).hexdigest()
        cutout=self.root/'large-cutout.png';Image.new('RGBA',(800,800),(0,0,0,0)).save(cutout)
        # 给透明图中央放入可识别主体，确保白底转换不是空图。
        with Image.open(cutout) as im:
            canvas=im.copy();canvas.paste((30,60,90,255),(180,160,620,690));canvas.save(cutout)
        with session_scope(self.root) as s:
            asset=s.get(ProductAsset,'large');asset.cutout_path='large-cutout.png';asset.sha256=digest
            s.add(AssetAuthorization(authorization_id='mode-auth',asset_id='large',asset_sha256=digest,
                confirmed=True,statement='test',confirmed_at='now'))
        plan={'plan_id':'mode-plan','slots':[dict(section='轮播图',index=1,purpose='正面商品展示',
            source_asset_ids=['large'],prompt='生成干净商品图')]}
        common=(patch('pintan.media_pipeline._platform_record',return_value=(SimpleNamespace(platform='淘宝'),
                SimpleNamespace(product_id='p'),{},1)),patch('pintan.content_flow.scan',return_value=[]),
                patch('pintan.database.product_bundle',return_value={}),patch('pintan.image_workflow.plan_images',return_value=plan))
        with common[0],common[1],common[2],common[3],patch('pintan.image_workflow.generate_candidate',return_value=raw) as gen:
            generate_planned(self.root,'mode-conservative',audit_level='none',generation_mode='conservative')
            conservative_path=gen.call_args.args[1][0]
        self.assertEqual(conservative_path.resolve(),(self.root/'large.png').resolve())
        # patch对象不能重复进入，第二次创建一组新的上下文。
        with patch('pintan.media_pipeline._platform_record',return_value=(SimpleNamespace(platform='淘宝'),SimpleNamespace(product_id='p'),{},1)),patch('pintan.content_flow.scan',return_value=[]),patch('pintan.database.product_bundle',return_value={}),patch('pintan.image_workflow.plan_images',return_value=plan),patch('pintan.image_workflow.generate_candidate',return_value=raw) as gen:
            generate_planned(self.root,'mode-creative',audit_level='none',generation_mode='creative')
            creative_path=gen.call_args.args[1][0]
        self.assertNotEqual(creative_path.resolve(),(self.root/'large.png').resolve())
        self.assertTrue(creative_path.name.endswith('-white.jpg'))
        with session_scope(self.root) as s:
            media=s.scalar(select(GeneratedMedia).where(GeneratedMedia.content_id=='mode-creative'))
            meta=json.loads(media.metadata_json)
        self.assertEqual(meta['generation_mode'],'creative')
        self.assertEqual(meta['model_input_kind'],'cutout_white_background')
        self.assertEqual(meta['model_input_asset_ids'],['large'])

    def test_text_only_concept_first_attempt_sends_no_images(self):
        raw=(self.root/'large.png').read_bytes()
        plan={'plan_id':'concept-plan','slots':[dict(section='详情页',index=1,purpose='厨房痛点场景',
            source_asset_ids=['large'],prompt='痛点引入')]}
        with patch('pintan.media_pipeline._platform_record',return_value=(SimpleNamespace(platform='淘宝'),SimpleNamespace(product_id='p'),{},1)),patch('pintan.content_flow.scan',return_value=[]),patch('pintan.database.product_bundle',return_value={}),patch('pintan.image_workflow.plan_images',return_value=plan),patch('pintan.image_workflow.generate_candidate',return_value=raw) as gen:
            generate_planned(self.root,'mode-concept',audit_level='none',generation_mode='creative',
                             slot_modes={'详情页-1':'conservative'})
        self.assertEqual(gen.call_args.args[1],[])
        with session_scope(self.root) as s:
            media=s.scalar(select(GeneratedMedia).where(GeneratedMedia.content_id=='mode-concept'))
            meta=json.loads(media.metadata_json)
        self.assertEqual(meta['generation_mode'],'text_only_concept')
        self.assertEqual(meta['generation_mode_source'],'automatic')
        self.assertEqual(meta['model_input_kind'],'none')

    def test_one_slot_preflight_failure_does_not_stop_other_slots(self):
        raw=(self.root/'large.png').read_bytes();digest=hashlib.sha256(raw).hexdigest()
        with session_scope(self.root) as s:
            s.get(ProductAsset,'large').sha256=digest
            s.add(AssetAuthorization(authorization_id='isolation-auth',asset_id='large',asset_sha256=digest,
                confirmed=True,statement='test',confirmed_at='now'))
        plan={'plan_id':'isolation-plan','slots':[
            dict(section='轮播图',index=1,purpose='未授权图',source_asset_ids=['small'],prompt='主体'),
            dict(section='轮播图',index=2,purpose='已授权图',source_asset_ids=['large'],prompt='主体')]}
        with patch('pintan.media_pipeline._platform_record',return_value=(SimpleNamespace(platform='淘宝'),SimpleNamespace(product_id='p'),{},1)),patch('pintan.content_flow.scan',return_value=[]),patch('pintan.database.product_bundle',return_value={}),patch('pintan.image_workflow.plan_images',return_value=plan),patch('pintan.image_workflow.generate_candidate',return_value=raw) as gen:
            result=generate_planned(self.root,'isolation-content',audit_level='none')
        self.assertEqual([x['status'] for x in result['items']],['generation_failed','success'])
        self.assertEqual(gen.call_count,1)

    def test_group_generation_uses_independent_parallel_slot_calls(self):
        plan={'plan_id':'parallel-plan','slots':[
            dict(section='轮播图',index=1,purpose='首图',source_asset_ids=['large'],prompt='首图'),
            dict(section='轮播图',index=2,purpose='侧面',source_asset_ids=['large'],prompt='侧面')]}
        lock=threading.Lock();active=0;peak=0;calls=[];progress=[]
        def one_slot(root,content_id,slots,**kwargs):
            nonlocal active,peak
            with lock:
                active+=1;peak=max(peak,active);calls.append(tuple(slots))
            time.sleep(.05)
            with lock:active-=1
            return {'items':[{'slot':slots[0],'status':'success'}]}
        with patch.dict('os.environ',{'PINTAN_IMAGE_WORKERS':'2'}),\
             patch('pintan.image_workflow.plan_images',return_value=plan),\
             patch('pintan.image_workflow._generate_planned_serial',side_effect=one_slot):
            result=generate_planned(self.root,'parallel-content',audit_level='none',
                                    progress_callback=lambda done,total,workers:progress.append((done,total,workers)))
        self.assertEqual(peak,2)
        self.assertCountEqual(calls,[('轮播图-1',),('轮播图-2',)])
        self.assertEqual(result['parallel_workers'],2)
        self.assertEqual(progress[0],(0,2,2));self.assertEqual(progress[-1],(2,2,2))

if __name__=='__main__':unittest.main()
