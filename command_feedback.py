"""One thread-safe voice turn indicator, independent of Tk and playback."""

import threading
import time


class CommandFeedback:
    def __init__(self):
        self.lock = threading.Lock()
        self.turn = 0
        self.source = "voice"
        self.accepted = False
        self.value = {}

    def begin(self, stage="addressed", text="", source="voice"):
        with self.lock:
            self.turn += 1
            self.source = source
            self.accepted = False
            self._set(stage, text, 8)
            return self.turn

    def update(self, turn, stage, text="", seconds=8):
        with self.lock:
            # A slow old action may finish after the user has given a new one.
            if turn != self.turn:
                return
            self._set(stage, text, seconds)

    def _set(self, stage, text, seconds):
        if stage in ("queued", "executing", "success", "error"):
            self.accepted = True
        self.value = dict(
            stage=stage,
            text=str(text)[:160],
            until=time.time() + seconds,
            turn=self.turn,
            source=self.source,
            accepted=self.accepted,
        )

    def snapshot(self):
        with self.lock:
            return dict(self.value) if self.value.get("until", 0) > time.time() else {}
