"""Bounded, local microphone rehearsal. Final transcripts never execute actions."""

import threading
import time

from command_understanding import exact_command
from speech_recognition import command_confidence, decode_voice_command


class HearingPractice:
    PHRASES = ("переключи трек", "выключи микрофон", "сделай скриншот")

    def __init__(self, manager, settings):
        self.manager, self.settings = manager, settings
        self.lock = threading.RLock()
        self.until = 0
        self.index = 0
        self.rows = []
        self.message = "Проверка ещё не запускалась. Звук не записывается."

    def _expire(self):
        if self.until and time.monotonic() >= self.until:
            self.until = 0
            self.message = "Время проверки истекло. Обычные голосовые команды снова включены."

    def call(self, action):
        with self.lock:
            if action == "start":
                self.index, self.rows = 0, []
                self.until = time.monotonic() + 90
                self.message = "Произнесите фразу ниже. Действия во время проверки заблокированы."
            elif action == "stop":
                self.until = 0
                self.message = "Проверка остановлена. Обычные голосовые команды снова включены."
            else:
                raise ValueError("Неизвестное действие проверки слуха")
            return self.snapshot()

    def snapshot(self):
        with self.lock:
            self._expire()
            wake = self.settings().get("app_settings", {}).get("wake_word", "крест")
            return {
                "active": bool(self.until),
                "remaining": max(0, int(self.until - time.monotonic())),
                "expected": wake + " " + self.PHRASES[self.index] if self.until else "",
                "message": self.message,
                "passed": self.index,
                "rows": [dict(row) for row in self.rows],
            }

    def consume(self, text, recognition):
        with self.lock:
            self._expire()
            if not self.until:
                return False
            cfg = self.settings()
            addressed, body = decode_voice_command(self.manager, text, cfg)
            # Background chat neither executes nor consumes a test attempt.
            if not addressed:
                return True
            if body in ("закончи проверку", "останови проверку"):
                self.call("stop")
                return True
            if not body:
                self.message = "Обращение услышано. Теперь произнесите обращение вместе с командой."
                return True
            command = exact_command(self.manager, body)
            match = self.manager.match(command) if command else None
            expected = self.manager.match(self.PHRASES[self.index])
            score = command_confidence(
                self.manager,
                text,
                body,
                addressed,
                recognition.get("confidence"),
                recognition,
                cfg.get("app_settings", {}),
            )
            passed = bool(
                match
                and expected
                and match[0].id == expected[0].id
                and match[1] == expected[1]
                and (
                    score is None or score * 100 >= cfg["app_settings"].get("speech_confidence", 60)
                )
            )
            self.rows.append(
                {
                    "heard": text[:300],
                    "expected": self.PHRASES[self.index],
                    "passed": passed,
                    "confidence": score,
                    "pause_ms": recognition.get("pause_ms"),
                    "audio_queue_ms": recognition.get("audio_queue_ms"),
                    "decoder_ms": recognition.get("decoder_ms"),
                }
            )
            if passed:
                self.index += 1
            self.message = (
                "Верно. Следующая фраза:" if passed else "Не совпало. Повторите фразу ниже."
            )
            if self.index == len(self.PHRASES) or len(self.rows) >= 12:
                self.until = 0
                self.message = (
                    f"Проверка завершена: {self.index} из {len(self.PHRASES)} фраз. "
                    "Обычные голосовые команды снова включены."
                )
            return True
