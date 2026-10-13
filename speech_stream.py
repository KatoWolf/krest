"""Bounded, fixed-duration PCM framing, independent of PortAudio callback sizes."""

from collections import deque
import threading

from speech_tools import pcm_levels


class AudioFrames:
    def __init__(self, rate=16000, milliseconds=30, capacity=80):
        self.size = rate * milliseconds // 1000 * 2
        self.seconds = milliseconds / 1000
        self.capacity = capacity
        self.lock = threading.Lock()
        self.frames = deque()
        self.tail = b""
        self.generation = 0
        self.dropped = 0
        self.ready = threading.Event()

    def put(self, data, captured, overflow=False):
        with self.lock:
            if overflow:
                self.frames.clear()
                self.tail = b""
                self.generation += 1
                self.dropped += 1
            data = self.tail + data
            count = len(data) // self.size
            # Keep the newest sound; never accumulate seconds of stale commands.
            if len(self.frames) + count > self.capacity:
                self.dropped += len(self.frames) + max(0, count - self.capacity)
                self.generation += 1
                self.frames.clear()
            for index in range(max(0, count - self.capacity), count):
                frame = data[index * self.size : (index + 1) * self.size]
                at = captured - (len(data) - (index + 1) * self.size) / (self.size / self.seconds)
                self.frames.append((frame, pcm_levels(frame)[0], at, self.generation))
            self.tail = data[count * self.size :]
            if self.frames:
                self.ready.set()

    def take(self, maximum=32):
        with self.lock:
            packets = [self.frames.popleft() for _ in range(min(maximum, len(self.frames)))]
            if not self.frames:
                self.ready.clear()
            return packets

    def clear(self):
        with self.lock:
            self.frames.clear()
            self.tail = b""
            self.ready.clear()


class Continuity:
    """After a lost chunk require a quiet boundary, never join two half-commands."""

    def __init__(self):
        self.generation = 0
        self.recovering = False
        self.quiet = 0.0

    def accept(self, generation, rms, threshold, seconds):
        changed = generation != self.generation
        if changed:
            self.generation = generation
            self.recovering = True
            self.quiet = 0.0
        if self.recovering:
            self.quiet = self.quiet + seconds if rms < threshold else 0.0
            if self.quiet >= 0.18:
                self.recovering = False
            return False, changed
        return True, changed
