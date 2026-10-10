"""Shared command runtime; all UI callbacks consume events on the Tk thread."""

from pathlib import Path
import logging
import queue
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
    from speech_tools import split_wake

    return split_wake(text, wake_word)


class AssistantRuntime:
    def __init__(self, settings, state=None, data_dir=BASE_DIR):
        self.settings = settings
        self.state = state or AssistantState(Path(data_dir) / "assistant_state.json")
        self.state.config_getter = settings
        self.manager = SkillManager(settings)
        self.state.skill_manager = self.manager
        self.events = queue.Queue()
        self.requests = queue.Queue(maxsize=30)
        self._request_lock = threading.RLock()
        self.current_command = ""
        self.notifications = NotificationQueue(Path(data_dir) / "notifications.sqlite3")
        self.stop_event = threading.Event()
        self._threads = []
        self._voice_thread = None
        self._voice_control = threading.Lock()
        self._voice_stop = threading.Event()
        self._active_until = 0
        self._arm_requested = threading.Event()
        self.download_status = "Автосортировка выключена"
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
            threading.Thread(target=self._downloads, daemon=True, name="krest-downloads"),
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, text, source="text", confidence=None):
        text = text.strip()
        if not text:
            return False
        if len(text) > 2000:
            self.events.put(("error", "Команда слишком длинная: максимум 2000 символов."))
            return False
        with self._request_lock:
            if self.stop_event.is_set():
                return False
            try:
                self.requests.put_nowait((text, source, confidence))
                return True
            except queue.Full:
                pass
        self.events.put(("error", "Очередь команд заполнена. Дождитесь завершения текущих команд."))
        return False

    def clear_pending(self):
        """Cancel only commands that have not started; never interrupt a skill midway."""
        with self._request_lock:
            count = 0
            while True:
                try:
                    self.requests.get_nowait()
                    self.requests.task_done()
                    count += 1
                except queue.Empty:
                    return count

    def command_status(self):
        with self._request_lock:
            return self.current_command, self.requests.qsize()

    def execute(self, text, source="text", confidence=None):
        cfg = self.settings().get("app_settings", {})
        wake, command = check_wake_word(text, cfg.get("wake_word", "крест"))
        from speech_tools import normalize_command

        def observed(status):
            if source == "voice":
                self.events.put(
                    (
                        "recognition",
                        {
                            "heard": text,
                            "command": normalize_command(
                                command, self.settings().get("command_aliases", {})
                            ),
                            "confidence": confidence,
                            "status": status,
                        },
                    )
                )

        if source == "voice":
            if not wake and time.monotonic() > self._active_until:
                observed("Без обращения — команда не выполнена")
                return CommandResult(False, False, "")
            if confidence is not None and confidence * 100 < cfg.get("speech_confidence", 60):
                observed("Низкая уверенность — повторите фразу")
                result = CommandResult(
                    True, False, "Не уверена, что расслышала правильно. Повторите команду."
                )
                voice.speak(result.text)
                self.events.put(("result", result))
                return result
            if wake:
                self._active_until = time.monotonic() + cfg.get("active_timeout", 6)
            if not command and wake:
                observed("Ожидаю команду")
                voice.speak("Слушаю.")
                return CommandResult(True, True, "Слушаю.")
        else:
            command = command if wake else text
        if self.state.is_sleeping and normalize_command(
            command, self.settings().get("command_aliases", {})
        ).casefold().strip() not in (
            "проснись",
            "активация",
            "вставай",
            "подъем",
            "подъём",
        ):
            observed("Режим сна")
            result = CommandResult(True, False, "Ассистент спит. Скажите «крест проснись».")
            self.events.put(("result", result))
            return result
        from skills import gid

        if gid.dota_state.waiting_for_user_answer:
            if command.strip().casefold() in ("да", "давай", "конечно"):
                command = "включи гид"
            elif command.strip().casefold() in ("нет", "не надо", "отмена"):
                command = "отмена гида"
        observed("Передано на выполнение")
        result = self.manager.execute(command, self.state)
        observed("Выполнено" if result.success else "Не выполнено: " + result.text[:160])
        self.last_result = result
        if result.text and not result.responded:
            voice.speak(result.text)
        self.events.put(("result", result))
        return result

    def _commands(self):
        while not self.stop_event.is_set():
            with self._request_lock:
                if self.stop_event.is_set():
                    break
                try:
                    text, source, confidence = self.requests.get_nowait()
                except queue.Empty:
                    text = None
                if text is not None:
                    self.current_command = text
            if text is None:
                self.stop_event.wait(0.1)
                continue
            try:
                if confidence is None:
                    self.execute(text, source)
                else:
                    self.execute(text, source, confidence=confidence)
            except Exception as exc:
                logger.exception("Сбой обработки команды")
                self.events.put(("error", str(exc)))
            finally:
                with self._request_lock:
                    self.current_command = ""
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
                    self.events.put(("notification", notification))
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

    def _downloads(self):
        from downloads import DownloadSorter

        sorter = DownloadSorter()
        last_run = 0
        previous = None
        last_error = ""
        while not self.stop_event.is_set():
            cfg = self.settings().get("app_settings", {})
            enabled = cfg.get("downloads_auto_sort", False) and self.settings().get(
                "skills", {}
            ).get("system_mon", True)
            signature = (enabled, cfg.get("downloads_path", ""))
            if signature != previous:
                sorter = DownloadSorter()
                last_run = 0
                previous = signature
                last_error = ""
            if enabled and time.monotonic() - last_run >= 30:
                last_run = time.monotonic()
                try:
                    result = sorter.scan(signature[1], stop=self.stop_event)
                    self.download_status = f"Проверено в {time.strftime('%H:%M:%S')} · перемещено: {result['moved_count']} · ожидают: {result['skipped_count']}"
                    if result["moved_count"]:
                        self.events.put(("automation", "Загрузки: " + self.download_status))
                    error = "; ".join(result["errors"][:3])
                    if error:
                        self.download_status += " · ошибки: " + error
                except (OSError, ValueError) as exc:
                    error = str(exc)
                    self.download_status = "Сортировка: " + error
                if error and error != last_error:
                    self.events.put(("error", "Автосортировка: " + error))
                last_error = error
            elif not enabled:
                self.download_status = "Автосортировка выключена"
            self.stop_event.wait(1)

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
                if self._arm_requested.is_set():
                    self._arm_requested.clear()
                    self._active_until = time.monotonic() + self.settings().get(
                        "app_settings", {}
                    ).get("active_timeout", 6)
                    self.events.put(("voice", "Слушаю команду без обращения"))
                text = voice.listen()
                if text:
                    self.submit(text, "voice", voice.recognition().get("confidence"))
        except Exception as exc:
            self.events.put(("error", f"Голос: {exc}"))
        finally:
            with self._voice_control:
                voice.disable()
            self.events.put(("voice", "Голос остановлен"))

    def arm_voice(self):
        voice.stop_speaking()
        self._arm_requested.set()
        self.start_voice()

    def stop_voice(self):
        self._voice_stop.set()
        self._arm_requested.clear()
        self._active_until = 0
        voice.stop_speaking()

    def close(self):
        with self._request_lock:
            self.stop_event.set()
            self.clear_pending()
        self.stop_voice()
        from skills import gid

        gid.stop_service()
        for thread in self._threads:
            thread.join(timeout=1)
        self.state.persist()
