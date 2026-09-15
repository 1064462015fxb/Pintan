"""在临时数据库验证价格语义、证据关系、迁移与事实发布，不调用模型。"""
import json
import tempfile
import unittest
from pathlib import Path
from sqlalchemy import select, func
from pintan.database import (Base, engine, session_scope, OfferSnapshot, FactEvidence,
    OfferField, ProductClaim, CollectedSku, CollectionRun, AssetRequirement, product_bundle)
from pintan.collector import collect_snapshot, confirm_run
from pintan.collector_mapping import parse_mapping, backfill_mappings, read_mapping, save_review

def block(region,text):return {'region':region,'text':text,'source_locator':'fixture.'+region}

class MappingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        Base.metadata.create_all(engine(self.root))
        self.snapshot={'page_url':'https://detail.1688.com/offer/123.html','title':'桌边垃圾袋',
            'text_blocks':[block('title','桌边垃圾袋'),block('attributes','材质：PE\n类目：垃圾袋\n功能：自粘\n尺寸：25×30cm'),
                           block('price','起批量：1包'),
                           block('price','1-9包 ￥1.70\n10包以上 ￥0.65'),
                           block('supplier','公司名称：测试有限公司\n经营年限：5年\n复购率：30%\n近30天销量：0件')],
            'skus':[{'text':'10个装\n价格：1.70\n库存：20包','source_locator':'sku.0','image_url':'https://cbu01.alicdn.com/a.jpg',
                     'dimension':'颜色','image_relation_type':'selected_main_image','displayed_image_changed':True}]}
    def tearDown(self):self.tmp.cleanup()
    def test_scoped_prices_units_exact_signals_and_sku(self):
        m=parse_mapping(self.snapshot,'R1')
        self.assertEqual([t['price_cny'] for t in m['price_tiers']],[1.7,.65])
        self.assertEqual(m['price_tiers'][0]['unit'],'包')
        self.assertEqual(m['numeric']['sales_30d'],0)
        self.assertNotIn('sales_30d',m['selection_input']['missing_fields'])
        self.assertEqual(m['skus'][0]['stock'],20)
        self.assertEqual(m['skus'][0]['dimension'],'颜色')
        self.assertEqual(m['skus'][0]['image_relation_type'],'selected_main_image')
        self.assertFalse(m['selection_input']['ready'])
        eids={e['evidence_id'] for e in m['evidence']}
        self.assertTrue(all(f['evidence_id'] in eids for f in m['fields']+m['price_tiers']+m['skus']))
    def test_no_minimum_price_guess_or_ambiguous_sales_conversion(self):
        sample={'text_blocks':[block('price','￥7-12\n已售：1万+\n近30天销量：100+件'),block('supplier','复购率：130%')],
                'skus':[{'text':'白色\n价格：7-12','source_locator':'SKU'}]}
        m=parse_mapping(sample,'R')
        self.assertEqual(m['price_tiers'],[]);self.assertIsNone(m['numeric']['sales_30d'])
        self.assertIsNone(m['numeric']['repurchase_rate_pct']);self.assertIsNone(m['skus'][0]['price_cny'])

    def test_split_decimal_and_sku_region_prices_are_preserved(self):
        sample={'text_blocks':[
            block('price','新人价\n¥\n4\n.83\n首件预估到手价'),
            block('price','¥\n5\n.83\n¥\n12\n.10\n1件起批'),
            block('sku','颜色\n车载垃圾袋-30只\n¥5.83\n库存9946件\n车载垃圾袋-90只\n¥12.1\n库存9912件\n车载垃圾袋-60只\n¥8.97\n库存9969件')],
            'skus':[]}
        m=parse_mapping(sample,'R-split')
        # 无法绑定具体规格的展示/促销价格只保留在原始证据，不再形成单独价格字段。
        self.assertFalse(any(f['field_name']=='price_text' for f in m['fields']))
        self.assertEqual([(x['text'],x['price_cny'],x['stock']) for x in m['skus']],
                         [('车载垃圾袋-30只',5.83,9946),('车载垃圾袋-90只',12.1,9912),
                          ('车载垃圾袋-60只',8.97,9969)])
        self.assertFalse(any(x['value']=='颜色：车载垃圾袋-30只' for x in m['product_features']))

    def test_collection_persists_skus_recovered_from_sku_text_region(self):
        sample={'page_url':'https://detail.1688.com/offer/456.html','title':'车载垃圾袋',
            'text_blocks':[block('title','车载垃圾袋'),
                block('price','¥\n5\n.83\n¥\n12\n.10\n1件起批'),
                block('sku','颜色\n30只装\n¥5.83\n库存9946件\n90只装\n¥12.1\n库存9912件')],
            'skus':[]}
        out=collect_snapshot(self.root,sample,False)
        with session_scope(self.root) as s:
            rows=list(s.scalars(select(CollectedSku).where(CollectedSku.run_id==out['run_id'])
                                .order_by(CollectedSku.price_cny)))
            self.assertEqual([(json.loads(x.spec_json)['text'],x.price_cny,x.stock) for x in rows],
                             [('30只装',5.83,9946),('90只装',12.1,9912)])
    def test_title_is_not_company_and_conflicts_stay_separate(self):
        sample={'text_blocks':[block('title','测试有限公司'),block('attributes','材质：PE'),block('detail','材质：HDPE')]}
        m=parse_mapping(sample,'R')
        self.assertIsNone(m['resolved']['title']);self.assertEqual(m['resolved']['supplier_name'],'测试有限公司')
        self.assertIsNone(m['resolved']['material'])
        self.assertEqual(m['conflicts'][0]['values'],['HDPE','PE'])
        self.assertTrue(all(c['usage_status']=='证据冲突，禁止引用' for c in m['content_claims']))
    def test_persist_confirmation_and_new_snapshot_do_not_mix_claims(self):
        out=collect_snapshot(self.root,self.snapshot,False)
        with session_scope(self.root) as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(ProductClaim)),0)
            sku=s.scalar(select(CollectedSku));self.assertEqual(sku.price_cny,1.7)
            self.assertEqual(sku.image_url,'https://cbu01.alicdn.com/a.jpg')
            persisted_spec=json.loads(sku.spec_json)
            self.assertTrue(s.get(FactEvidence,persisted_spec['evidence_id']))
            self.assertEqual(persisted_spec['dimension'],'颜色')
            self.assertEqual(persisted_spec['image_relation_type'],'selected_main_image')
        confirm_run(self.root,out['run_id'])
        bundle=product_bundle(self.root,out['product_id'])
        self.assertTrue(any(c['value']=='PE' for c in bundle['claims']))
        newer=dict(self.snapshot,text_blocks=[block('title','新版垃圾袋'),
            block('attributes','类目：垃圾袋\n材质：HDPE'),block('price','起批量：1包')])
        out2=collect_snapshot(self.root,newer,False)
        # 时间戳同秒仍固定测试顺序，以免run_id随机排序影响该断言。
        with session_scope(self.root) as s:s.get(CollectionRun,out2['run_id']).created_at='2099-01-01'
        confirm_run(self.root,out2['run_id'])
        bundle=product_bundle(self.root,out['product_id'])
        self.assertFalse(any(c['value']=='PE' for c in bundle['claims']))
        self.assertTrue(any(c['value']=='HDPE' for c in bundle['claims']))
        with session_scope(self.root) as s:
            self.assertTrue(s.scalar(select(ProductClaim).where(ProductClaim.value=='PE')))
    def test_old_snapshot_backfill_is_idempotent_and_does_not_overwrite(self):
        # 模拟仅有旧CollectionRun的数据库。
        from pintan.database import Product
        with session_scope(self.root) as s:
            s.add(Product(product_id='1688-123',title='旧商品',category=''));s.flush()
            s.add(CollectionRun(run_id='CR-legacy',page_url=self.snapshot['page_url'],offer_id='123',
                product_id='1688-123',status='待复核',raw_snapshot_json=json.dumps(dict(title='旧商品',visible_text='材质：PE')),
                normalized_json='{}',created_at='2026-01-01',updated_at='2026-01-01'))
        backfill_mappings(self.root)
        with session_scope(self.root) as s:
            before=s.scalar(select(func.count()).select_from(OfferField))
        backfill_mappings(self.root);backfill_mappings(self.root)
        with session_scope(self.root) as s:
            self.assertEqual(s.scalar(select(func.count()).select_from(OfferField)),before)
            self.assertTrue(read_mapping(s,'CR-legacy')['legacy_snapshot'])
            self.assertEqual(s.scalar(select(func.count()).select_from(AssetRequirement)),4)
    def test_transaction_rollback(self):
        out=collect_snapshot(self.root,self.snapshot,False)
        with self.assertRaises(RuntimeError):
            with session_scope(self.root) as s:
                s.get(OfferSnapshot,out['run_id']).payload='broken'
                raise RuntimeError('rollback')
        with session_scope(self.root) as s:self.assertTrue(read_mapping(s,out['run_id'])['fields'])

    def test_review_can_exclude_edit_and_add_before_publish(self):
        out=collect_snapshot(self.root,self.snapshot,False)
        with session_scope(self.root) as s:
            mapped=read_mapping(s,out['run_id']);items=[]
            for f in mapped['review_fields']:
                if f['field_name']=='material':f={**f,'included':False}
                items.append(f)
            items.append({'field_name':'material','value':'HDPE','included':True})
            save_review(s,out['run_id'],items)
        confirm_run(self.root,out['run_id']);bundle=product_bundle(self.root,out['product_id'])
        self.assertFalse(any(c['value']=='PE' for c in bundle['claims']))
        hdpe=next(c for c in bundle['claims'] if c['value']=='HDPE')
        self.assertEqual(hdpe['source_field'],'manual-review:'+out['run_id'])

    def test_confirmation_requires_a_reviewed_product_title(self):
        out=collect_snapshot(self.root,self.snapshot,False)
        with session_scope(self.root) as s:
            items=read_mapping(s,out['run_id'])['review_fields']
            for item in items:
                if item['field_name']=='title':item['included']=False
            save_review(s,out['run_id'],items)
        with self.assertRaisesRegex(ValueError,'商品标题'):confirm_run(self.root,out['run_id'])

    def test_confirmation_requires_all_common_attributes(self):
        sample={'page_url':'https://detail.1688.com/offer/789.html','title':'垃圾袋',
                'text_blocks':[block('title','垃圾袋'),block('price','1件起批')]}
        out=collect_snapshot(self.root,sample,False)
        with self.assertRaisesRegex(ValueError,'类目.*材质'):confirm_run(self.root,out['run_id'])

    def test_review_automatically_merges_semantically_equal_values(self):
        out=collect_snapshot(self.root,self.snapshot,False)
        with session_scope(self.root) as s:
            items=read_mapping(s,out['run_id'])['review_fields']
            # 模拟网页重复区块和人工补录造成的完全重复/标点形式重复。
            items.extend([
                {'field_name':'material','value':' PE ','included':True},
                {'field_name':'product_features','value':'颜色：黑色, 深灰、蓝色','included':True},
                {'field_name':'product_features','value':'颜色：黑色、深灰，蓝色','included':True},
            ])
            save_review(s,out['run_id'],items)
            reviewed=read_mapping(s,out['run_id'])['review_fields']
            material=[x for x in reviewed if x['field_name']=='material' and x['value'].strip()=='PE']
            colors=[x for x in reviewed if x['field_name']=='product_features' and x['value'].startswith('颜色：')]
            self.assertEqual(len(material),1);self.assertEqual(material[0]['duplicate_count'],2)
            self.assertEqual(len(colors),1);self.assertEqual(colors[0]['duplicate_count'],2)

    def test_mapping_upgrade_archives_old_result_and_preserves_raw_snapshot(self):
        from pintan.database import OfferMappingHistory
        out=collect_snapshot(self.root,self.snapshot,False)
        with session_scope(self.root) as s:
            row=s.get(OfferSnapshot,out['run_id']);row.mapping_version='legacy-test';old=row.payload
            raw=s.get(CollectionRun,out['run_id']).raw_snapshot_json
        backfill_mappings(self.root);backfill_mappings(self.root)
        with session_scope(self.root) as s:
            self.assertEqual(s.get(OfferMappingHistory,(out['run_id'],'legacy-test')).payload,old)
            self.assertEqual(s.get(CollectionRun,out['run_id']).raw_snapshot_json,raw)

    def test_ui_control_and_nonphysical_size_are_not_product_parameters(self):
        m=parse_mapping({'text_blocks':[block('sku','尺寸\n充电款\n尺寸\n展开已售罄商品')]},'R')
        self.assertFalse(any(x['value'].startswith('尺寸：') for x in m['product_features']))
        self.assertEqual(m['content_claims'],[])

    def test_supplier_card_metrics_are_grouped_with_labels(self):
        m=parse_mapping({'text_blocks':[block('supplier',
            '义乌市奕於贸易有限公司\n入驻2年\n主营：居家日用\n品质达标率 100%\n48小时支撑率 98%\n店铺回头率 35%')]},'R-shop')
        self.assertEqual(m['resolved']['supplier_name'],'义乌市奕於贸易有限公司')
        self.assertEqual(m['resolved']['supplier_years'],'2年')
        values={x['value'] for x in m['supplier_info'] if x['field_name']=='supplier_metrics'}
        self.assertTrue({'主营：居家日用','品质达标率：100%','48小时支撑率：98%','店铺回头率：35%'}.issubset(values))

    def test_logistics_services_and_promotions_keep_their_scope(self):
        text=('限时8折 包邮 首单减1元且免运费 先采后付0元下单\n'
              '浙江金华 送至 云南保山 承诺48小时发货 包邮\n'
              '敢赔付 品质不符包赔 严选晚发必赔 退货包运费 7天无理由退货')
        m=parse_mapping({'text_blocks':[block('supplier',text)]},'R-logistics')
        self.assertEqual(m['resolved']['shipping_origin'],'浙江金华')
        self.assertNotIn('云南保山',json.dumps(m['supplier_info'],ensure_ascii=False))
        self.assertEqual(m['resolved']['delivery_time'],'48小时')
        services={x['value'] for x in m['supplier_info'] if x['field_name']=='supplier_services'}
        self.assertIn('品质不符包赔',services);self.assertIn('7天无理由退货',services)
        promotions={x['value'] for x in m['supplier_info'] if x['field_name']=='promotion_terms'}
        self.assertEqual(promotions,{'限时8折','首单减1元且免运费','先采后付0元下单'})

    def test_nested_regions_do_not_duplicate_fields_and_reviews_do_not_define_specs(self):
        m=parse_mapping({'text_blocks':[block('attributes','材质：PE'),block('detail','材质：PE'),
            block('reviews','材质：纸\n使用后有异味')]},'R')
        materials=[x for x in m['fields'] if x['field_name']=='material']
        self.assertEqual(len(materials),1);self.assertEqual(materials[0]['value'],'PE')
        self.assertEqual([x['text'] for x in m['review_candidates']],['使用后有异味'])

if __name__=='__main__':unittest.main()
