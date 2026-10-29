"""One serial stdlib executor with finite, nonblocking submission admission."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import BoundedSemaphore


class WorkQueueFull(ValueError):
    def __init__(self):
        super().__init__('WORK_QUEUE_FULL')


class BoundedExecutor:
    def __init__(self, *, capacity=8, thread_name_prefix='quantagent'):
        self._capacity = BoundedSemaphore(capacity)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name_prefix)

    @contextmanager
    def admission(self):
        if not self._capacity.acquire(blocking=False):
            raise WorkQueueFull()
        consumed = False

        def submit(fn, *args, **kwargs):
            nonlocal consumed
            if consumed:
                raise ValueError('SUBMISSION_ALREADY_USED')
            future = self._executor.submit(fn, *args, **kwargs)
            consumed = True
            future.add_done_callback(lambda _: self._capacity.release())
            return future

        try:
            yield submit
        finally:
            if not consumed:
                self._capacity.release()

    def submit(self, fn, *args, **kwargs):
        with self.admission() as submit:
            return submit(fn, *args, **kwargs)

    def shutdown(self, **kwargs):
        self._executor.shutdown(**kwargs)
