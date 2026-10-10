"""Five-second, memory-only microphone test. Does not load Vosk or Torch."""

import threading
from speech_tools import pcm_levels


class MicrophoneTest:
    def __init__(self):
        self.level = 0
        self.peak = 0
        self.clipped = False
        self.samples = b""
        self.noise_threshold = None
        self.busy = False
        self.stop = threading.Event()
        self._lock = threading.Lock()

    def record(self, device="", calibrate=False):
        with self._lock:
            if self.busy:
                raise RuntimeError("Проверка микрофона уже идёт")
            self.busy = True
            self.stop.clear()
        self.level = self.peak = 0
        self.clipped = False
        self.samples = b""
        self.noise_threshold = None
        parts, levels = [], []
        dropped = 0
        try:
            import sounddevice as sd

            def capture(data, frames, timing, status):
                nonlocal dropped
                if status:
                    dropped += 1
                rms, peak = pcm_levels(bytes(data))
                self.level, self.peak = rms, peak
                self.clipped = self.clipped or peak >= 32700
                if len(parts) < 110:
                    parts.append(bytes(data))
                    levels.append(rms)

            selected = int(device) if str(device).isdigit() else device or None
            with sd.RawInputStream(
                device=selected,
                channels=1,
                dtype="int16",
                samplerate=16000,
                blocksize=800,
                callback=capture,
            ):
                self.stop.wait(5)
            if self.stop.is_set():
                return "Проверка отменена"
            if dropped:
                raise RuntimeError(
                    "Звук поступал с пропусками. Повторите проверку при меньшей нагрузке."
                )
            if not parts:
                raise RuntimeError("Микрофон не вернул звук")
            if calibrate:
                typical = sorted(levels)[min(len(levels) - 1, int(len(levels) * 0.9))]
                self.noise_threshold = min(5000, max(50, int(typical * 2 + 30)))
                return f"Рекомендуемый порог речи: {self.noise_threshold}. Проверьте его обычной командой."
            self.samples = b"".join(parts)
            return "Запись готова. " + (
                "Есть перегрузка: уменьшите усиление микрофона."
                if self.clipped
                else "Нажмите «Прослушать запись»."
            )
        finally:
            self.busy = False

    def playback(self):
        if not self.samples:
            raise RuntimeError("Сначала запишите тестовую фразу")
        import sounddevice as sd

        with sd.RawOutputStream(channels=1, dtype="int16", samplerate=16000) as output:
            output.write(self.samples)
