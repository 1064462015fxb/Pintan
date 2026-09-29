"""15秒短视频分镜、并行镜头和成片的离线测试。"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from pintan.database import Base, GeneratedMedia, Product, VideoPlan, engine, session_scope, utcnow
from pintan.video_workflow import generate_video, plan_video, video_plan


class VideoWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        Base.metadata.create_all(engine(self.root))
        folder=self.root/'data/media/p/generated/c'
        folder.mkdir(parents=True)
        with session_scope(self.root) as s:
            s.add(Product(product_id='p',title='桌边垃圾袋',category='家居用品'))
            for index,color in enumerate(('red','green','blue'),1):
                path=folder/f'image-{index}.png';Image.new('RGB',(360,360),color).save(path)
                s.add(GeneratedMedia(media_id=f'i{index}',product_id='p',content_id='c',media_type='image',
                    slot=f'轮播图-{index}',version=1,status='success',local_path=str(path.relative_to(self.root)),
                    sha256=f'sha{index}',metadata_json=json.dumps({'content_version':1,
                    'generation_mode':'conservative'}),created_at=utcnow()))

    def tearDown(self):self.tmp.cleanup()

    def platform(self):
        return (SimpleNamespace(platform='淘宝'),SimpleNamespace(product_id='p'),
            {'商品标题':'桌边垃圾袋','视频钩子':'桌面垃圾随手收纳','核心卖点':['免弯腰','随手即丢']},1)

    def test_plan_has_exactly_five_factual_three_second_shots(self):
        with patch('pintan.video_workflow._platform_record',return_value=self.platform()),\
             patch('pintan.video_workflow._ai_layout',return_value=(None,{'source':'rules-fallback'})):
            result=plan_video(self.root,'c','conservative','1:1')
        self.assertEqual(result['duration_seconds'],15)
        self.assertEqual([x['duration_seconds'] for x in result['shots']],[3]*5)
        self.assertEqual(result['shots'][0]['caption'],'桌面垃圾随手收纳')
        self.assertEqual(len(result['shots']),5)
        with session_scope(self.root) as s:self.assertIsNotNone(s.get(VideoPlan,result['plan_id']))

    def test_two_worker_render_produces_versioned_clips_and_15_second_video(self):
        with patch('pintan.video_workflow._platform_record',return_value=self.platform()),\
             patch('pintan.video_workflow._ai_layout',return_value=(None,{'source':'rules-fallback'})),\
             patch('pintan.video_workflow._tts_audio',return_value=(None,'test-silent')),\
             patch.dict('pintan.video_workflow.SIZES',{'1:1':(320,320)}):
            plan=plan_video(self.root,'c','conservative','1:1')
            calls=[]
            result=generate_video(self.root,'c',plan['plan_id'],progress_callback=lambda *x:calls.append(x))
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['success_count'],5)
        self.assertEqual(calls[-1][:3],(5,5,2))
        with session_scope(self.root) as s:
            clips=s.query(GeneratedMedia).filter_by(content_id='c',media_type='video_clip').all()
            final=s.get(GeneratedMedia,result['media_id'])
        self.assertEqual(len(clips),5);self.assertTrue((self.root/final.local_path).is_file())
        self.assertEqual(json.loads(final.metadata_json)['duration_seconds'],15)
        current=video_plan(self.root,plan['plan_id'])
        self.assertTrue(all(x['clip_url'] for x in current['shots']))

    def test_failed_shot_can_be_retried_without_deleting_successful_clips(self):
        with patch('pintan.video_workflow._platform_record',return_value=self.platform()),\
             patch('pintan.video_workflow._ai_layout',return_value=(None,{'source':'rules-fallback'})):
            plan=plan_video(self.root,'c','conservative','1:1')
        original=__import__('pintan.video_workflow',fromlist=['_clip'])._clip
        def flaky(root,plan_value,shot,source,version):
            if shot['index']==3:raise RuntimeError('fixture failure')
            return original(root,plan_value,shot,source,version)
        with patch('pintan.video_workflow.SIZES',{'1:1':(320,320)}),\
             patch('pintan.video_workflow._clip',side_effect=flaky),\
             patch('pintan.video_workflow._tts_audio',return_value=(None,'silent')):
            first=generate_video(self.root,'c',plan['plan_id'])
        self.assertEqual(first['status'],'partial');self.assertEqual(first['failed_count'],1)
        with patch('pintan.video_workflow.SIZES',{'1:1':(320,320)}),\
             patch('pintan.video_workflow._tts_audio',return_value=(None,'silent')):
            second=generate_video(self.root,'c',plan['plan_id'],[3])
        self.assertEqual(second['status'],'completed')
        with session_scope(self.root) as s:
            self.assertEqual(s.query(GeneratedMedia).filter_by(content_id='c',media_type='video_clip',status='success').count(),5)


if __name__=='__main__':unittest.main()
