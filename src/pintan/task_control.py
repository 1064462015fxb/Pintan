"""Cooperative cancellation, including transaction publication and child threads."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading


class TaskCancelled(RuntimeError):
    pass


class Cancellation:
    def __init__(self):
        self.event = threading.Event()
        self.lock = threading.RLock()

    def cancel(self):
        with self.lock:
            self.event.set()

    def check(self):
        if self.event.is_set():
            raise TaskCancelled('任务已取消；已完成的历史结果保留')


current = ContextVar('pintan_cancellation', default=None)


def checkpoint():
    token = current.get()
    if token:
        token.check()


@contextmanager
def publication():
    """Cancellation and transaction commit share a lock: no late publication."""
    token = current.get()
    if token is None:
        yield
    else:
        with token.lock:
            token.check()
            yield
