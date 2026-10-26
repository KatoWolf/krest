"""Small priority queues: user input first, ageing prevents starvation."""

import itertools
import queue
import time


def source_priority(source):
    if source in {"voice", "text"}:
        return 0
    if source == "scenario" or source.startswith("brain:"):
        return 20
    return 10


class CommandQueue(queue.Queue):
    def _init(self, maxsize):
        self.queue = []
        self.serial = itertools.count()

    def _put(self, item):
        self.queue.append((source_priority(item[1]), next(self.serial), time.monotonic(), item))

    def _get(self):
        now = time.monotonic()
        index = min(
            range(len(self.queue)),
            key=lambda i: (
                max(0, self.queue[i][0] - int((now - self.queue[i][2]) / 10) * 10),
                self.queue[i][1],
            ),
        )
        return self.queue.pop(index)[3]

    def remove_job(self, ident):
        with self.not_full:
            for index, row in enumerate(self.queue):
                if row[3][3] == ident:
                    self.queue.pop(index)
                    self.unfinished_tasks -= 1
                    if not self.unfinished_tasks:
                        self.all_tasks_done.notify_all()
                    self.not_full.notify()
                    return True
        return False
