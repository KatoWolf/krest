"""Cooperative cancellation: never kill a thread in the middle of a file mutation."""

import threading
import time


class ExecutionToken:
    def __init__(self, shutdown, timeout=300):
        self.shutdown = shutdown
        self.cancelled = threading.Event()
        self.deadline = time.monotonic() + timeout

    def is_set(self):
        return (
            self.cancelled.is_set() or self.shutdown.is_set() or time.monotonic() >= self.deadline
        )

    def set(self):
        self.cancelled.set()

    @property
    def reason(self):
        if time.monotonic() >= self.deadline:
            return "Превышено время выполнения"
        return "Остановка запрошена"
