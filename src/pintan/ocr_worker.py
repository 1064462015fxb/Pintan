"""Paddle runs outside the API process; native GIL stalls are killable here."""
import atexit
import io
import multiprocessing
import os
import threading
import time

from .task_control import checkpoint


def paddle_worker(connection):
    # Set before importing numpy/Paddle. Do not consume all desktop CPU cores.
    os.environ['OMP_NUM_THREADS'] = '2'
    os.environ['MKL_NUM_THREADS'] = '2'
    engine = None
    try:
        while True:
            connection.send(('ready', None))
            data = connection.recv_bytes()
            try:
                import numpy as np
                from PIL import Image
                from paddleocr import PaddleOCR
                if engine is None:
                    engine = PaddleOCR(use_doc_orientation_classify=False,
                        use_doc_unwarping=False, use_textline_orientation=False,
                        lang='ch', enable_mkldnn=False, cpu_threads=2)
                with Image.open(io.BytesIO(data)) as source:
                    image = source.convert('RGB')
                    image.thumbnail((2400, 4800))
                    output = list(engine.predict(np.array(image)))
                texts = []
                for page in output:
                    value = getattr(page, 'json', page)
                    if isinstance(value, dict):
                        value = value.get('res', value)
                        if isinstance(value, dict):
                            texts.extend(value.get('rec_texts', []))
                connection.send((True, '\n'.join(str(x) for x in texts if str(x).strip())[:8000]))
            except Exception as exc:
                # No model credentials or full requests in errors.
                connection.send((False, type(exc).__name__))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


class IsolatedOCR:
    def __init__(self, target=paddle_worker):
        self.target = target
        self.lock = threading.Lock()
        self.process = None
        self.connection = None

    def close(self):
        if self.process is not None:
            if self.process.pid is not None:
                if self.process.is_alive():
                    self.process.terminate()
                self.process.join(timeout=2)
                if self.process.is_alive():
                    self.process.kill()
                    self.process.join(timeout=2)
            self.process.close()
        if self.connection is not None:
            self.connection.close()
        self.process = self.connection = None

    def recognize(self, data, timeout=90, queue_timeout=120):
        deadline = time.monotonic() + queue_timeout
        while not self.lock.acquire(timeout=0.1):
            checkpoint()
            if time.monotonic() >= deadline:
                raise TimeoutError('OCR排队超时，可重试；原图保留')
        try:
            checkpoint()
            deadline = time.monotonic() + timeout
            def receive():
                while not self.connection.poll(0.1):
                    checkpoint()
                    if time.monotonic() >= deadline:
                        raise TimeoutError('OCR识别超时，已终止独立识别进程；原图保留')
                    if not self.process.is_alive():
                        raise RuntimeError('OCR识别进程已退出，可重试')
                checkpoint()
                return self.connection.recv()
            if self.process is None or not self.process.is_alive():
                self.close()
                ctx = multiprocessing.get_context('spawn')
                parent, child = ctx.Pipe()
                self.process = ctx.Process(target=self.target, args=(child,), daemon=True,
                                           name='pintan-ocr')
                self.connection = parent
                self.process.start()
                child.close()
            # Handshake includes cold process startup in the deadline, before a
            # potentially large image is sent into a bounded OS pipe.
            ready, _ = receive()
            if ready != 'ready':
                raise RuntimeError('OCR进程通信状态异常')
            self.connection.send_bytes(data)
            ok, result = receive()
            if not ok:
                raise RuntimeError('OCR识别失败：' + result)
            return result
        except BaseException:
            self.close()
            raise
        finally:
            self.lock.release()


worker = IsolatedOCR()
atexit.register(worker.close)
