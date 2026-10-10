"""Shared command runtime; all UI callbacks consume events on the Tk thread."""

from difflib import SequenceMatcher
from pathlib import Path
import logging
import queue
import re
import threading
import time

import voice
from app_state import AssistantState
from notifications import NotificationQueue
from skill_manager import SkillManager, CommandResult
from storage import atomic_write_json

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


def check_wake_word(text, wake_word):
    match = re.match(r"\s*([\w]+)[\s,.:!?-]*(.*)", text, re.S)
    if not match or not wake_word:
        return False, text
    first, remainder = match.groups()
    if (
        first.casefold() == wake_word.casefold()
        or SequenceMatcher(None, first.casefold(), wake_word.casefold()).ratio() >= 0.8
    ):
        return True, remainder.strip()
    return False, text


class AssistantRuntime:
    def __init__(self, settings, state=None, data_dir=BASE_DIR):
        self.settings = settings
        self.state = state or AssistantState(Path(data_dir) / "assistant_state.json")
        self.state.config_getter = settings
        self.manager = SkillManager(settings)
        self.state.skill_manager = self.manager
        self.events = queue.Queue()
        self.requests = queue.Queue(maxsize=30)
        self.notifications = NotificationQueue(Path(data_dir) / "notifications.sqlite3")
        self.stop_event = threading.Event()
        self._threads = []
        self._voice_thread = None
        self._voice_control = threading.Lock()
        self._voice_stop = threading.Event()
        self._active_until = 0
        self.last_result = None
        self.status_path = Path(data_dir) / "current_status.json"
        voice.configure(self._speech_event, settings, self.state)

    def _speech_event(self, text):
        self.events.put(("speech", text))

    def start(self):
        if self._threads:
            return
        self._threads = [
            threading.Thread(target=self._commands, daemon=True, name="krest-commands"),
            threading.Thread(target=self._scheduler, daemon=True, name="krest-scheduler"),
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, text, source="text"):
        text = text.strip()
        if not text:
            return False
        try:
            self.requests.put_nowait((text, source))
            return True
        except queue.Full:
            self.events.put(
                (
                    "error",
                    "Очередь команд заполнена. Дождитесь завершения текущих команд.",
                )
            )
            return False

    def execute(self, text, source="text"):
        cfg = self.settings().get("app_settings", {})
        wake, command = check_wake_word(text, cfg.get("wake_word", "крест"))
        if source == "voice":
            if not wake and time.monotonic() > self._active_until:
                return CommandResult(False, False, "")
            if wake:
                self._active_until = time.monotonic() + cfg.get("active_timeout", 6)
            if not command and wake:
                voice.speak("Слушаю.")
                return CommandResult(True, True, "Слушаю.")
        else:
            command = command if wake else text
        if self.state.is_sleeping and command.casefold().strip() not in (
            "проснись",
            "активация",
            "вставай",
            "подъем",
            "подъём",
        ):
            result = CommandResult(True, False, "Ассистент спит. Скажите «крест проснись».")
            self.events.put(("result", result))
            return result
        from skills import gid

        if gid.dota_state.waiting_for_user_answer:
            if command.strip().casefold() in ("да", "давай", "конечно"):
                command = "включи гид"
            elif command.strip().casefold() in ("нет", "не надо", "отмена"):
                command = "отмена гида"
        result = self.manager.execute(command, self.state)
        self.last_result = result
        if result.text and not result.responded:
            voice.speak(result.text)
        self.events.put(("result", result))
        return result

    def _commands(self):
        while not self.stop_event.is_set():
            try:
                text, source = self.requests.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.execute(text, source)
            except Exception as exc:
                logger.exception("Сбой обработки команды")
                self.events.put(("error", str(exc)))
            finally:
                self.requests.task_done()

    def _scheduler(self):
        while not self.stop_event.wait(0.3):
            try:
                for item in self.state.take_due():
                    voice.speak(f"Время вышло: {item.get('label', 'Напоминание')}.")
                    self.events.put(("alarm", item))
                try:
                    notification = self.notifications.get_nowait()
                except queue.Empty:
                    notification = None
                if notification:
                    voice.speak(f"Новое сообщение: {notification}")
                from skills import gid

                self.state.dota_guide_active = gid.dota_state.is_active
                status = {
                    "is_playing": gid.dota_state.dota_running,
                    "game": "Dota 2" if gid.dota_state.dota_running else "",
                    "activity_type": "GAME" if gid.dota_state.dota_running else "IDLE",
                    "full_message": "Режим сна" if self.state.is_sleeping else "Ожидание команды",
                    "updated_at": time.time(),
                }
                if not hasattr(self, "_published") or time.monotonic() - self._published >= 2:
                    atomic_write_json(self.status_path, status)
                    self._published = time.monotonic()
                if self.state.shutdown_requested.is_set():
                    self.events.put(("shutdown", None))
                    return
            except Exception as exc:
                logger.exception("Сбой планировщика")
                self.events.put(("error", str(exc)))
                self.stop_event.wait(2)

    def start_voice(self):
        if self._voice_thread and self._voice_thread.is_alive():
            return
        self._voice_stop.clear()
        self._voice_thread = threading.Thread(
            target=self._voice_loop, daemon=True, name="krest-listen"
        )
        self._voice_thread.start()

    def _voice_loop(self):
        try:
            with self._voice_control:
                voice.enable()
            self.events.put(("voice", "Микрофон активен"))
            while not self.stop_event.is_set() and not self._voice_stop.wait(0.03):
                text = voice.listen()
                if text:
                    self.submit(text, "voice")
        except Exception as exc:
            self.events.put(("error", f"Голос: {exc}"))
        finally:
            with self._voice_control:
                voice.disable()
            self.events.put(("voice", "Голос остановлен"))

    def stop_voice(self):
        self._voice_stop.set()

    def close(self):
        self.stop_event.set()
        self.stop_voice()
        from skills import gid

        gid.stop_service()
        for thread in self._threads:
            thread.join(timeout=1)
        self.state.persist()
