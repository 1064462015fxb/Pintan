"""No paid calls: kill a hung OCR worker and cancel a late model publication."""
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks
from fastapi.testclient import TestClient
from pintan.database import Base, ImagePlan, MediaTask, engine, session_scope, utcnow
from pintan.ocr_worker import IsolatedOCR
from pintan.task_control import Cancellation, TaskCancelled, current


def fake_ocr(connection):
    while True:
        try:
            connection.send(('ready',None))
            data=connection.recv_bytes()
        except EOFError:
            return
        if data==b'hang':
            # Represents native code holding the child interpreter's GIL.
            while True:pass
        connection.send((True,data.decode()))


def startup_hang(connection):
    while True:pass


class ResponsivenessTests(unittest.TestCase):
    def test_timeout_also_covers_worker_startup(self):
        worker=IsolatedOCR(startup_hang)
        try:
            start=time.monotonic()
            with self.assertRaises(TimeoutError):worker.recognize(b'x'*1000000,timeout=.5)
            self.assertLess(time.monotonic()-start,4)
        finally:worker.close()

    def test_ocr_timeout_kills_worker_and_recovers(self):
        worker=IsolatedOCR(fake_ocr)
        try:
            self.assertEqual(worker.recognize(b'warm',timeout=10),'warm')
            start=time.monotonic()
            with self.assertRaises(TimeoutError):worker.recognize(b'hang',timeout=.3)
            self.assertLess(time.monotonic()-start,4)
            self.assertIsNone(worker.process)
            self.assertEqual(worker.recognize(b'recovered',timeout=10),'recovered')
        finally:worker.close()

    def test_ocr_cancel_kills_child(self):
        worker=IsolatedOCR(fake_ocr);token=Cancellation()
        try:
            worker.recognize(b'warm',timeout=10)
            with ThreadPoolExecutor(max_workers=1) as pool:
                def run():
                    ctx=current.set(token)
                    try:return worker.recognize(b'hang',timeout=20)
                    finally:current.reset(ctx)
                future=pool.submit(run)
                time.sleep(.2);token.cancel()
                with self.assertRaises(TaskCancelled):future.result(timeout=4)
            self.assertIsNone(worker.process)
        finally:worker.close()

    def test_api_cancel_blocks_late_publication_and_status_overwrite(self):
        from pintan.webapp import create_app
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);Base.metadata.create_all(engine(root))
            with patch('pintan.webapp.init_database'), \
                 patch('pintan.collector_mapping.backfill_mappings'), \
                 patch('pintan.stable.score_and_promote_candidates'), \
                 patch('pintan.collector_bridge.install_collector_bridge'):
                app=create_app(root)
            @app.post('/test-work')
            def test_work(background:BackgroundTasks):
                return app.state.submit_media(background,'image-plan',{'content_id':'C'})
            entered=threading.Event();release=threading.Event()
            def late_plan(*args,**kwargs):
                entered.set();release.wait(10)
                with session_scope(root) as s:
                    s.add(ImagePlan(plan_id='late',content_id='C',input_hash='late',payload='{}',created_at=utcnow()))
                return {'slots':[]}
            with TestClient(app) as client, patch('pintan.image_workflow.plan_images',side_effect=late_plan):
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future=pool.submit(client.post,'/test-work')
                    try:
                        self.assertTrue(entered.wait(5))
                        self.assertEqual(client.get('/api/health').json(),{'status':'ok'})
                        task_id=client.get('/api/media/tasks?content_id=C').json()['items'][0]['task_id']
                        reply=client.post('/api/media/tasks/'+task_id+'/cancel').json()
                        self.assertEqual(reply['status'],'cancelled')
                    finally:release.set()
                    future.result(timeout=5)
                self.assertEqual(client.get('/api/media/tasks/'+task_id).json()['status'],'cancelled')
                self.assertEqual(client.get('/api/media/tasks?content_id=C').json()['items'],[])
            with session_scope(root) as s:
                self.assertIsNone(s.get(ImagePlan,'late'))
                self.assertEqual(s.get(MediaTask,task_id).status,'cancelled')


if __name__=='__main__':unittest.main()
