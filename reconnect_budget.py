"""Bounded microphone reconnects; hardware arrival or explicit setup re-arms them."""

import time


class ReconnectBudget:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.attempts = []
        self.healthy_since = 0

    def reset(self):
        self.attempts.clear()
        self.healthy_since = 0

    def remaining(self):
        now = self.clock()
        self.attempts = [at for at in self.attempts if now - at < 600]
        if len(self.attempts) >= 3:
            return max(0, self.attempts[0] + 600 - now)
        if self.attempts:
            return max(0, self.attempts[-1] + (3 if len(self.attempts) == 1 else 10) - now)
        return 0

    def allow(self):
        if self.remaining():
            return False
        self.attempts.append(self.clock())
        self.healthy_since = 0
        return True

    def healthy(self):
        if not self.healthy_since:
            self.healthy_since = self.clock()
        elif self.clock() - self.healthy_since >= 60:
            self.attempts.clear()
