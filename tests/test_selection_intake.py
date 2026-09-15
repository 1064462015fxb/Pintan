"""采集确认、SKU计价与内容交接回归；只用临时库，不调用付费模型。"""
import json,tempfile,unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path
from pintan.database import Base,engine,read_business,session_scope,BusinessRow,product_bundle
from pintan.collector import collect_snapshot,confirm_run
from pintan.selection_intake import update_scoring_inputs
from pintan.data_store import STABLE_FILE

class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        Base.metadata.create_all(engine(self.root))
        snap={'page_url':'https://detail.1688.com/offer/123.html','title':'垃圾袋',
          'text_blocks':[{'region':'title','text':'桌边自粘垃圾袋','source_locator':'title'},
                         {'region':'attributes','text':'类目：垃圾袋\n材质：PE\n功能：自粘','source_locator':'attributes'},
                         {'region':'price','text':'起批量：1包','source_locator':'price'}],
          'skus':[{'text':'30只','price_cny':5.83,'source_locator':'sku.0'},
                  {'text':'60只','price_cny':8.97,'source_locator':'sku.1'}]}
        self.run=collect_snapshot(self.root,snap,False)
    def tearDown(self):self.temp.cleanup()
    def selection_row(self,pid):
        with session_scope(self.root) as s:
            row=s.get(BusinessRow,('stable',pid)) or s.get(BusinessRow,('candidate',pid))
            return json.loads(row.payload)
    def test_confirmation_enrolls_once_without_fabricated_score(self):
        confirm_run(self.root,self.run['run_id']);confirm_run(self.root,self.run['run_id'])
        rows=read_business(self.root,STABLE_FILE);self.assertEqual(rows,[])
        row=self.selection_row(self.run['product_id'])
        self.assertEqual(row.get('total_score',''),'')
        self.assertEqual(row['wholesale_price_cny'],'')
        self.assertIn('selection_context',product_bundle(self.root,self.run['product_id']))
    def test_manual_inputs_validation_and_exact_sku_price(self):
        confirm_run(self.root,self.run['run_id']);pid=self.run['product_id']
        with self.assertRaises(ValueError):update_scoring_inputs(self.root,pid,{'supplier_quality_proxy_pct':101})
        with self.assertRaises(ValueError):update_scoring_inputs(self.root,pid,{'sales_30d':'NaN'})
        with self.assertRaises(ValueError):update_scoring_inputs(self.root,pid,{'wholesale_price_cny':1})
        # 明确固定真实采集价格对，验证所选规格而非最低价参与计算。
        with session_scope(self.root) as s:
            row=s.get(BusinessRow,{'kind':'candidate','row_id':pid});data=json.loads(row.payload)
            data['spec_price_pairs']=json.dumps([{'text':'30只','price_cny':5.83},{'text':'60只','price_cny':8.97}]);row.payload=json.dumps(data)
        payload={'selected_specification':'60只','feature_tags':'自粘','sales_30d':100,
                 'repurchase_rate_pct':20,'estimated_shipping_cny':2,
                 'supplier_quality_proxy_pct':60,'sku_reference_prices':{'30只':15,'60只':20}}
        first=update_scoring_inputs(self.root,pid,payload)[0]
        second=update_scoring_inputs(self.root,pid,payload)[0]
        self.assertEqual(first['wholesale_price_cny'],'8.97')
        self.assertEqual(first['total_score'],second['total_score'])
        self.assertEqual(first['score_status'],'完整参考评分')
        partial=update_scoring_inputs(self.root,pid,{'sales_30d':''})[0]
        self.assertEqual(partial['total_score'],'')
        self.assertEqual(partial['heat_score'],'')
        self.assertEqual(partial['score_coverage_pct'],70)
        self.assertEqual(float(partial['margin_score']),float(first['margin_score']))

    def test_ai_estimation_cache_and_manual_price_survives(self):
        from pintan.stable_estimation import estimate_stable
        confirm_run(self.root,self.run['run_id']);pid=self.run['product_id']
        with session_scope(self.root) as s:
            row=s.get(BusinessRow,('candidate',pid));data=json.loads(row.payload)
            data.update(selected_specification='30只',wholesale_price_cny='5.83',estimated_shipping_cny='0',
                spec_price_pairs=json.dumps([{'text':'30只','price_cny':5.83},{'text':'60只','price_cny':8.97}]))
            row.payload=json.dumps(data)
        response=dict(sku_prices=[
            dict(specification='30只',retail_price=12.9,price_low=9.9,price_high=15.9),
            dict(specification='60只',retail_price=18.9,price_low=15.9,price_high=21.9)],supplier_score=None,
            reason='商品SKU成本估算',evidence_ids=[],missing_metrics=['商家资料'],confidence=.6)
        with patch('pintan.stable_estimation.refresh_inputs',side_effect=lambda *a:{**self.selection_row(pid),'_selection_kind':'candidate'}),patch('pintan.model_config.load_model_config',return_value=SimpleNamespace(model='test',api_key='test',base_url='https://example.com')),patch('langchain_openai.ChatOpenAI') as model:
            model.return_value.invoke.return_value=SimpleNamespace(content=json.dumps(response))
            first=estimate_stable(self.root,pid)
            self.assertTrue(estimate_stable(self.root,pid)['cached'])
            self.assertEqual(model.return_value.invoke.call_count,1)
            changed=update_scoring_inputs(self.root,pid,{'sku_reference_prices':{'30只':8,'60只':10}})
            before=self.selection_row(pid)
            self.assertLess(float(changed[0]['margin_score']),100)
            estimate_stable(self.root,pid,force=True)
        data=self.selection_row(pid)
        self.assertEqual(data['sku_reference_prices'],{'30只':8.0,'60只':10.0})
        self.assertEqual(float(data['ai_reference_retail_price_cny']),15.9)
        self.assertIsInstance(data['ai_estimate'],dict)

    def test_all_skus_are_scored_and_product_uses_average(self):
        confirm_run(self.root,self.run['run_id']);pid=self.run['product_id']
        with session_scope(self.root) as s:
            row=s.get(BusinessRow,('candidate',pid));data=json.loads(row.payload)
            data['spec_price_pairs']=json.dumps([{'text':'30只','price_cny':5.83},{'text':'60只','price_cny':8.97}]);row.payload=json.dumps(data)
        values={'feature_tags':'自粘','sales_30d':100,'repurchase_rate_pct':20,'estimated_shipping_cny':0,
                'supplier_quality_proxy_pct':60,'sku_reference_prices':{'30只':12.9,'60只':18.9}}
        row=update_scoring_inputs(self.root,pid,values)[0]
        self.assertEqual(len(row['sku_scores']),2)
        self.assertTrue(all(x['profit_score']!='' for x in row['sku_scores']))
        expected=round(sum(float(x['profit_score']) for x in row['sku_scores'])/2,1)
        self.assertEqual(float(row['profit_score']),expected)

    def test_invalid_ai_price_retries_then_uses_rule_fallback(self):
        from pintan.stable_estimation import estimate_stable
        confirm_run(self.root,self.run['run_id']);pid=self.run['product_id']
        with session_scope(self.root) as s:
            row=s.get(BusinessRow,('candidate',pid));data=json.loads(row.payload)
            data['spec_price_pairs']=json.dumps([{'text':'30只','price_cny':5.83},{'text':'60只','price_cny':8.97}])
            row.payload=json.dumps(data,ensure_ascii=False)
        with patch('pintan.stable_estimation.refresh_inputs',side_effect=lambda *a:{**self.selection_row(pid),'_selection_kind':'candidate'}),patch('pintan.model_config.load_model_config',return_value=SimpleNamespace(model='test',api_key='test',base_url='https://example.com')),patch('langchain_openai.ChatOpenAI') as model:
            model.return_value.invoke.return_value=SimpleNamespace(content='{"bad":"format"}')
            estimate_stable(self.root,pid,force=True)
            self.assertEqual(model.return_value.invoke.call_count,2)
        data=self.selection_row(pid)
        self.assertEqual(data['ai_estimation_status'],'rules_fallback')
        self.assertEqual(set(data['sku_reference_prices']),{'30只','60只'})
        self.assertIn('规则兜底价',data['pricing_source'])
