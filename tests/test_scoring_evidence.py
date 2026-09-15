import json
import math
import tempfile
import unittest
from pathlib import Path

from pintan.database import Base, BusinessRow, engine, session_scope
from pintan.scoring_evidence import extract_signals
from pintan.stable import score_and_promote_candidates


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        Base.metadata.create_all(engine(self.root))

    def tearDown(self):self.tmp.cleanup()

    def add_candidate(self,row):
        with session_scope(self.root) as s:
            s.add(BusinessRow(kind='candidate',row_id=row['stable_id'],payload=json.dumps(row,ensure_ascii=False)))

    def test_display_lower_bound_and_store_rate(self):
        result=extract_signals('已售\n1.2万+件\n店铺回头率 35%\n包邮')
        self.assertEqual(result['sales_proxy']['value'],12000)
        self.assertEqual(result['sales_proxy']['period'],'未注明')
        self.assertTrue(result['sales_proxy']['lower_bound'])
        self.assertEqual(result['repurchase_proxy']['value'],35)
        self.assertEqual(result['shipping_proxy']['value'],0)

    def test_shipping_conditions(self):
        for text in ('退货包运费','满99元包邮','不包邮'):
            self.assertNotIn('shipping_proxy',extract_signals(text))

    def test_three_pillar_formula_and_proxy_sources(self):
        row=dict(stable_id='a',title='厨房收纳盒',category='家居收纳',feature_tags='收纳',
            spec_price_pairs=json.dumps([{'text':'小号','price_cny':5}]),sku_reference_prices={'小号':15},
            estimated_shipping_cny='',sales_30d='',repurchase_rate_pct='',
            scoring_evidence=extract_signals('已售100+件 店铺回头率35%'))
        self.add_candidate(row); scored=score_and_promote_candidates(self.root)[0]
        sales_score=round(20*math.log10(101),1)
        market=round(sales_score*.65+62.5*.35,1)
        self.assertEqual(scored['market_validation_score'],market)
        self.assertEqual(scored['score_coverage_pct'],100)
        self.assertEqual(scored['score_version'],'stable-rules-v5-three-pillars')
        self.assertEqual(scored['total_score'],round(scored['profit_score']*.4+market*.3+scored['operating_fit_score']*.3,1))
        self.assertEqual(scored['shipping_assumption'],'运费未采集，暂按0元估算')

    def test_missing_market_input_stays_candidate(self):
        row=dict(stable_id='b',title='桌面收纳盒',category='家居收纳',feature_tags='收纳',
            spec_price_pairs=json.dumps([{'text':'默认','price_cny':5}]),sku_reference_prices={'默认':15},
            sales_30d='100',repurchase_rate_pct='',estimated_shipping_cny='0')
        self.add_candidate(row)
        self.assertEqual(score_and_promote_candidates(self.root),[])
        with session_scope(self.root) as s:
            data=json.loads(s.get(BusinessRow,('candidate','b')).payload)
        self.assertEqual(data['market_validation_score'],'')
        self.assertIn('回头率',data['missing_score_fields'])
        self.assertEqual(data['promotion_status'],'needs_input')

if __name__=='__main__':unittest.main()
