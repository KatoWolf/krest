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
from performance import PerformancePolicy
from scenarios import ScenarioRunner
from notification_center import NotificationCenter, should_speak
from command_context import CommandContext
from platform_store import JobStore, MemoryStore

logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent


def check_wake_word(text, wake_word):
    from speech_tools import split_wake

    return split_wake(text, wake_word)


class AssistantRuntime:
    def __init__(self, settings, state=None, data_dir=BASE_DIR, config_manager=None):
        self.settings = settings
        from configuration import ConfigManager

        self.config_manager = config_manager or ConfigManager(Path(data_dir) / "config.json")
        self.state = state or AssistantState(Path(data_dir) / "assistant_state.json")
        self.state.config_getter = settings
        self.state.runtime = self
        self.jobs = JobStore(Path(data_dir) / "jobs.sqlite3")
        self.memory = MemoryStore(Path(data_dir) / "memory.sqlite3")
        self._job_local = threading.local()
        self._active_jobs = {}
        self._jobs_lock = threading.RLock()
        self.performance = PerformancePolicy(settings, Path(data_dir) / "activity_profile.json")
        self.center = NotificationCenter(Path(data_dir) / "inbox.sqlite3")
        from conversation_actions import ConversationActions

        self.conversations = ConversationActions(self)
        from desktop_context import DesktopContext

        self.desktop = DesktopContext(settings, Path(data_dir) / "file_index.sqlite3")
        from action_verifier import ActionVerifier

        self.verifier = ActionVerifier(self.publish_notice)
        from audio_focus import AudioFocus

        self.audio_focus = AudioFocus(settings, Path(data_dir) / "audio_restore.json")
        from personal_tools import ClipboardHistory, GameSessions

        self.clipboard = ClipboardHistory(Path(data_dir) / "clipboard.sqlite3", settings)
        self.game_sessions = GameSessions(Path(data_dir) / "game_sessions.sqlite3")
        self.context = CommandContext(self.state)
        from action_undo import UndoHistory
        from command_feedback import CommandFeedback

        self.undo = UndoHistory(self)
        self.feedback = CommandFeedback()
        self._feedback_turns = {}
        self.voice_status = "Микрофон на паузе"
        self.notification_mode = None
        self.scenarios = ScenarioRunner(self)
        from automation import EventAutomation

        self.automation = EventAutomation(self)
        self._execute_lock = threading.RLock()
        self._dialog_lock = threading.RLock()
        from command_lanes import LANES

        self._lane_locks = {name: threading.RLock() for name in LANES}
        self._lane_locks["general"] = self._execute_lock
        self.manager = SkillManager(settings, plugin_dir=Path(data_dir) / "imported_skills")
        from name_vocabulary import NameVocabulary

        self.manager.name_vocabulary = NameVocabulary()
        self.manager.name_vocabulary.update(
            settings().get("applications", []), [], settings().get("application_aliases", {})
        )
        from command_understanding import VoiceUnderstanding

        from companion_session import CompanionSession

        self.companion = CompanionSession(self, Path(data_dir))
        self.understanding = VoiceUnderstanding(self.manager, self.memory, self.companion)
        from hearing_practice import HearingPractice

        self.hearing = HearingPractice(self.manager, settings)
        self.state.skill_manager = self.manager
        self.events = queue.Queue()
        self.requests = queue.Queue(maxsize=30)
        self._queues = {name: queue.Queue(maxsize=12) for name in LANES}
        self._queues["general"] = self.requests
        self._queue_ready = {name: threading.Event() for name in LANES}
        self._running = {}
        self._request_lock = threading.RLock()
        self._submitted_at = {}
        self.current_command = ""
        self.notifications = NotificationQueue(Path(data_dir) / "notifications.sqlite3")
        self.stop_event = threading.Event()
        self._threads = []
        self._voice_thread = None
        self._voice_control = threading.Lock()
        self._voice_stop = threading.Event()
        self._active_until = 0
        self._last_voice_retry = float("-inf")
        self._arm_requested = threading.Event()
        self.download_status = "Автосортировка выключена"
        self.last_result = None
        self.status_path = Path(data_dir) / "current_status.json"
        from warframe_service import WarframeService

        self.warframe = WarframeService(self, data_dir)
        voice.configure(self._speech_event, settings, self.state)

    def _speech_event(self, text):
        self.events.put(("speech", text))

    def personal_tools(self, section, action, values=None):
        values = values or {}
        if section == "clipboard":
            if action == "enable":
                cfg = self.config_manager.snapshot()
                cfg["clipboard"]["enabled"] = bool(values.get("enabled"))
                self.config_manager.save_config(cfg)
                return (
                    "История включена."
                    if cfg["clipboard"]["enabled"]
                    else "Автоматическая история выключена."
                )
            return self.clipboard.call(action, **values)
        if section == "sessions":
            if action == "list":
                data = self.game_sessions.snapshot(values.get("id"))
                data["report"] = self.game_sessions.report(values.get("id"))
                return data
            if action == "note":
                return self.game_sessions.add(values.get("text", ""))
            if action == "open":
                return self.game_sessions.open_capture(values["id"])
        raise ValueError("Неизвестное действие личных инструментов")

    @property
    def voice_feedback(self):
        return self.feedback.snapshot()

    def skill_import(self, action, **args):
        imports = self.manager.imports
        if action == "list":
            result = imports.list()
            result["modules"] += self.manager.packages.list()
            return result
        if action == "preview":
            if Path(args["path"]).suffix.casefold() == ".zip":
                from extension_packages import inspect_package

                return inspect_package(args["path"])
            return imports.preview(args["path"])
        if action not in ("install", "enable", "disable", "remove", "settings"):
            raise ValueError("Неизвестное действие импорта")
        from contextlib import ExitStack

        with ExitStack() as stack:
            for lock in [self._dialog_lock, *self._lane_locks.values()]:
                if not lock.acquire(blocking=False):
                    raise ValueError("Дождитесь завершения текущей команды и повторите подключение")
                stack.callback(lock.release)
            result = (
                self.manager.packages.install(args["path"], args["sha256"])
                if action == "install" and Path(args["path"]).suffix.casefold() == ".zip"
                else self.manager.packages.change(args["id"][8:], action, args.get("values"))
                if str(args.get("id", "")).startswith("package-")
                else imports.install(args["path"], args["sha256"], args.get("bindings"))
                if action == "install"
                else imports.change(args["id"], action)
            )
            self.events.put(("skills_changed", None))
            return result

    def start(self):
        if self._threads:
            return
        self.manager.packages.event("assistant.started", {}, self)
        self._threads = [
            *[
                threading.Thread(
                    target=self._commands, args=(lane,), daemon=True, name="krest-" + lane
                )
                for lane in self._queues
            ],
            threading.Thread(target=self._scheduler, daemon=True, name="krest-scheduler"),
            threading.Thread(target=self._file_index_worker, daemon=True, name="krest-file-index"),
            threading.Thread(target=self._clipboard_worker, daemon=True, name="krest-clipboard"),
            threading.Thread(target=self._downloads, daemon=True, name="krest-downloads"),
            threading.Thread(target=self._catalog_worker, daemon=True, name="krest-catalog"),
        ]
        for thread in self._threads:
            thread.start()
        self.warframe.start()
        cfg = self.settings()
        if not cfg.get("safe_mode") and cfg.get("app_settings", {}).get("voice_auto_start", True):
            self.start_voice()

    def submit(
        self,
        text,
        source="text",
        confidence=None,
        request_key=None,
        *,
        recognition=None,
        feedback_turn=None,
    ):
        text = text.strip()
        if not text:
            return False
        # Emergency release must not wait behind network commands in the execution queue.
        from speech_tools import normalize_command

        _, stop_text = check_wake_word(
            text, self.settings().get("app_settings", {}).get("wake_word", "крест")
        )
        if normalize_command(stop_text or text) in (
            "верни управление",
            "останови маршрут",
            "я вернулся в игру",
        ):
            self.warframe.routes.cancel()
        if len(text) > 2000:
            self.events.put(("error", "Команда слишком длинная: максимум 2000 символов."))
            return False
        with self._request_lock:
            if self.stop_event.is_set():
                return False
            lane = self._command_lane(text)
            target = self._queues[lane]
            if target.full() or sum(q.qsize() for q in self._queues.values()) >= 30:
                self.events.put(("error", "Очередь заполнена"))
                return False
            job_id, fresh = self.jobs.create(text, source, request_key)
            if not fresh:
                return True
            try:
                target.put_nowait((text, source, confidence, job_id))
                self._queue_ready[lane].set()
                self._submitted_at[job_id] = (
                    time.monotonic(),
                    (recognition if recognition is not None else voice.recognition())
                    if source == "voice"
                    else {},
                )
                if source in ("voice", "text"):
                    turn = (
                        feedback_turn
                        if feedback_turn is not None
                        else self.feedback.begin("recognizing", text, source=source)
                    )
                    self._feedback_turns[job_id] = turn
                    self.feedback.update(turn, "queued", text)
                return True
            except queue.Full:
                self.jobs.transition(job_id, "cancelled", "Очередь заполнена")
        self.events.put(("error", "Очередь команд заполнена. Дождитесь завершения текущих команд."))
        return False

    def clear_pending(self):
        """Cancel only commands that have not started; never interrupt a skill midway."""
        with self._request_lock:
            count = 0
            for target in self._queues.values():
                while True:
                    try:
                        item = target.get_nowait()
                        self.jobs.transition(item[3], "cancelled", "Отменено до начала выполнения")
                        self._submitted_at.pop(item[3], None)
                        turn = self._feedback_turns.pop(item[3], None)
                        if turn is not None:
                            self.feedback.update(turn, "error", "Команда отменена", 3)
                        target.task_done()
                        count += 1
                    except queue.Empty:
                        break
            return count

    def command_status(self):
        with self._request_lock:
            return " · ".join(self._running.values()), sum(q.qsize() for q in self._queues.values())

    def _command_lane(self, text):
        from speech_recognition import decode_voice_command
        from command_understanding import exact_command

        _, body = decode_voice_command(self.manager, text, self.settings())
        from command_sequence import plan_sequence

        if plan_sequence(self.manager, body) is not None:
            return "sequence"
        command = exact_command(self.manager, body)
        found = self.manager.match(command) if command else None
        from command_lanes import lane_for

        return lane_for(found[0] if found else None)

    def execute(self, text, source="text", confidence=None):
        started = time.monotonic()
        ident = getattr(self._job_local, "current", None) or self.jobs.create(text, source)[0]
        submitted, recognition = self._submitted_at.pop(ident, (started, {}))
        self._job_local.recognition = recognition
        turn = self._feedback_turns.pop(ident, None)
        if turn is None and source in ("voice", "text"):
            turn = self.feedback.begin("recognizing", text, source=source)
        self._job_local.feedback_turn = turn
        self.feedback.update(turn, "recognizing", text)
        if not self.jobs.transition(ident, "running"):
            self.feedback.update(turn, "error", "Задание отменено", 3)
            return CommandResult(True, False, "Задание отменено или уже завершено")
        from execution import ExecutionToken

        token = ExecutionToken(self.stop_event)
        self._job_local.token = token
        with self._jobs_lock:
            self._active_jobs[ident] = token
        try:
            result = self._execute(text, source, confidence)
            status = "completed" if result.success else "cancelled" if token.is_set() else "failed"
            self.jobs.transition(ident, status, result.text)
            stage = "success" if result.success else "error"
            if source == "voice" and self._active_until > time.monotonic() and not result.skill_id:
                stage = "listening"
            if not result.handled and not result.text:
                stage = "idle"
            self.feedback.update(turn, stage, result.text, 6 if stage == "listening" else 3)
            trace = {
                "heard": text,
                "skill": result.skill_id,
                "success": result.success,
                "queue_ms": round((started - submitted) * 1000),
                "execution_ms": round((time.monotonic() - started) * 1000),
                "time": time.time(),
                "source": source,
            }
            if source == "voice":
                data = recognition or voice.recognition()
                trace.update(
                    {
                        key: data.get(key)
                        for key in ("decoder_ms", "pause_ms", "audio_queue_ms", "capture_ms")
                    }
                )
            if result.handled:
                with self.companion.lock:
                    self.companion.last_trace = trace
                self.events.put(("command_trace", trace))
            return result
        except Exception as exc:
            self.jobs.transition(ident, "cancelled" if token.is_set() else "failed", str(exc))
            self.feedback.update(turn, "error", str(exc), 4)
            raise
        finally:
            with self._jobs_lock:
                self._active_jobs.pop(ident, None)
            self._job_local.token = None
            self._job_local.recognition = None
            self._job_local.feedback_turn = None

    @property
    def command_stop(self):
        return getattr(self._job_local, "token", None) or self.stop_event

    def cancel_job(self, ident):
        with self._jobs_lock:
            token = self._active_jobs.get(ident)
            if token:
                token.set()
                return "Остановка запрошена. Уже отправленные действия не отменяются; проверяйте результат."
        job = self.jobs.get(ident)
        if (
            job
            and job["status"] == "queued"
            and self.jobs.transition(ident, "cancelled", "Отменено до выполнения")
        ):
            return "Задание отменено до выполнения"
        return "Задание уже завершено или не поддерживает остановку"

    def _voice_retry(self, addressed):
        # During the follow-up window, game chat must not start spoken tutorials.
        now = time.monotonic()
        if addressed and now - self._last_voice_retry >= 10:
            self._last_voice_retry = now
            voice.speak("Не расслышала. Повтори, пожалуйста.")

    def _prepare_command(self, text, source="text", confidence=None):
        cfg = self.settings().get("app_settings", {})
        if source == "voice":
            from speech_recognition import decode_voice_command

            wake, command = decode_voice_command(self.manager, text, self.settings())
        else:
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
            window_accepted = (getattr(self._job_local, "recognition", None) or {}).get(
                "listening_window", False
            )
            if not wake and not window_accepted and time.monotonic() > self._active_until:
                observed("Без обращения — команда не выполнена")
                return CommandResult(False, False, "")
            if not wake:
                from command_understanding import exact_command
                from command_sequence import plan_sequence

                answer = command.casefold().strip(" ,.!?")
                pending = self.understanding.pending
                is_answer = bool(
                    pending
                    and time.monotonic() < pending["until"]
                    and answer
                    in (
                        "да",
                        "нет",
                        "отмена",
                        "не надо",
                        "верно",
                        "правильно",
                        "подтверждаю",
                        "первое",
                        "первый",
                        "второе",
                        "второй",
                        "один",
                        "два",
                        "1",
                        "2",
                    )
                )
                is_context = bool(
                    self.context.pending
                    and time.monotonic() - self.context.pending["created"] < 120
                    and (
                        answer.isdigit()
                        or any(
                            v["label"].casefold() == answer for v in self.context.pending["choices"]
                        )
                    )
                )
                if not (
                    exact_command(self.manager, command)
                    or plan_sequence(self.manager, command)
                    or is_answer
                    or is_context
                ):
                    observed("Фоновая речь — ожидаю команду")
                    return CommandResult(False, False, "")
            from speech_recognition import command_confidence

            confidence = command_confidence(
                self.manager,
                text,
                command,
                wake,
                confidence,
                getattr(self._job_local, "recognition", None) or {},
                cfg,
            )
            if confidence is not None and confidence * 100 < cfg.get("speech_confidence", 60):
                observed("Низкая уверенность — повторите фразу")
                result = CommandResult(
                    True, False, "Не уверена, что расслышала правильно. Повторите команду."
                )
                self._voice_retry(wake)
                self.events.put(("result", result))
                return result
            if not command and wake:
                self._active_until = time.monotonic() + cfg.get("active_timeout", 6)
                observed("Ожидаю команду")
                from companion_behavior import policy

                text = policy(self.settings(), bool(self.performance.game))["listen"]
                voice.speak(text)
                return CommandResult(True, True, text)
            # A command consumes an explicitly opened listening window. Ordinary
            # conversation after a successful action never opens another one.
            self._active_until = 0
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
        learning = None
        if source in ("voice", "text"):
            from command_sequence import plan_sequence

            plan = plan_sequence(self.manager, command)
            if plan is not None:
                if source == "voice" and not plan.error:
                    from command_sequence import sequence_confidence

                    score = sequence_confidence(
                        plan, getattr(self._job_local, "recognition", None) or {}, confidence
                    )
                    if score is not None and score * 100 < cfg.get("speech_confidence", 60):
                        plan.error = (
                            "Не расслышала одну из команд. Цепочка не запущена; повторите её."
                        )
                if plan.error:
                    result = CommandResult(True, False, plan.error, "sequence")
                    return self._finish_command(result, None, source, wake, observed)
                observed("Цепочка проверена; передана на выполнение")
                return plan, None, None, wake, observed
        if source == "voice":
            import re

            correction = re.fullmatch(r"(?:я сказал|я сказала|я имел в виду) (.+)", command, re.I)
            if (
                correction
                and self.companion.last_voice
                and time.monotonic() - self.companion.last_voice_at < 60
            ):
                from command_understanding import exact_command

                corrected = exact_command(self.manager, correction[1])
                if corrected:
                    entry = self.manager.match(corrected)[0]
                    learning = (
                        self.companion.last_voice,
                        {"command": corrected, "skill": entry.id},
                    )
                    command = corrected
            if not learning and command.casefold() not in ("да", "нет", "первое", "второе"):
                self.companion.last_voice = command
                self.companion.last_voice_at = time.monotonic()
            interpreted = self.understanding.resolve(command, wake)
            if interpreted.prompt:
                result = CommandResult(True, False, interpreted.prompt)
                observed("Уточнение: " + interpreted.prompt)
                self._active_until = time.monotonic() + 25
                voice.speak(interpreted.prompt)
                self.last_result = result
                self.events.put(("result", result))
                return result
            command, learning = interpreted.command, interpreted.learning or learning
        observed("Передано на выполнение")
        normalized = normalize_command(command, self.settings().get("command_aliases", {}))
        # Context actions also mutate timers. Defer them until cancellation has
        # been checked and the resource lock / undo capture have been acquired.
        followup = normalized if source != "scenario" else None
        return command, followup, learning, wake, observed

    def _execute(self, text, source="text", confidence=None):
        # Dialog changes are short and serial; app/network calls run outside.
        with self._dialog_lock:
            prepared = self._prepare_command(text, source, confidence)
        if isinstance(prepared, CommandResult):
            return prepared
        command, followup, learning, wake, observed = prepared
        from command_sequence import SequencePlan

        if isinstance(command, SequencePlan):
            result = self._execute_sequence(command)
        else:
            result = self._execute_action(command, followup)
        with self._dialog_lock:
            return self._finish_command(result, learning, source, wake, observed)

    def _execute_action(self, command, followup=None):
        with self._lane_locks[self._command_lane(command)]:
            if self.command_stop.is_set():
                return CommandResult(True, False, "Задание отменено до отправки действия")
            self.feedback.update(
                getattr(self._job_local, "feedback_turn", None), "executing", command, 300
            )
            with self.undo.capture(command):
                if followup is not None:
                    with self._dialog_lock:
                        followup = self.context.handle(followup)
                return (
                    CommandResult(True, followup.success, followup.text)
                    if followup is not None
                    else self.manager.execute(command, self.state)
                )

    def _execute_sequence(self, plan):
        replies = []
        with voice.defer_output():
            for i, step in enumerate(plan.steps, 1):
                result = self._execute_action(step["command"])
                replies.append(f"{i}. {result.text}")
                if not result.success:
                    replies.append(
                        f"Цепочка остановлена на шаге {i}; следующие команды не выполнялись."
                    )
                    return CommandResult(True, False, "\n".join(replies), "sequence")
        return CommandResult(True, True, "\n".join(replies), "sequence")

    def _finish_command(self, result, learning, source, wake, observed):
        observed("Выполнено" if result.success else "Не выполнено: " + result.text[:160])
        if result.success and learning:
            self.understanding.remember(learning)
        if source == "voice":
            if result.success:
                self.companion.remember_topic(result.skill_id)
        self.last_result = result
        if result.text and not result.responded:
            if source == "voice" and not result.handled:
                self._voice_retry(wake)
            else:
                voice.speak(result.text)
        if result.skill_id.startswith("skills.media."):
            self.publish_notice(result.text, "Музыка", "low", speak=False)
        self.events.put(("result", result))
        return result

    def _commands(self, lane="general"):
        target = self._queues[lane]
        while not self.stop_event.is_set():
            with self._request_lock:
                if self.stop_event.is_set():
                    break
                try:
                    text, source, confidence, job_id = target.get_nowait()
                except queue.Empty:
                    text = None
                    self._queue_ready[lane].clear()
                if text is not None:
                    self._running[lane] = text
                    self.current_command = " · ".join(self._running.values())
            if text is None:
                self._queue_ready[lane].wait(0.25)
                continue
            try:
                self._job_local.current = job_id
                if confidence is None:
                    self.execute(text, source)
                else:
                    self.execute(text, source, confidence=confidence)
            except Exception as exc:
                logger.exception("Сбой обработки команды")
                self.events.put(("error", str(exc)))
            finally:
                self._job_local.current = None
                with self._request_lock:
                    self._running.pop(lane, None)
                    self.current_command = " · ".join(self._running.values())
                target.task_done()

    def _scheduler(self):
        while not self.stop_event.wait(0.3):
            try:
                for item in self.state.take_due():
                    self.manager.packages.event("timer.due", {"label": item.get("label", "")}, self)
                    self.publish_notice(
                        f"Время вышло: {item.get('label', 'Напоминание')}.", "Задачи", "high"
                    )
                    self.events.put(("alarm", item))
                try:
                    notification = self.notifications.get_nowait()
                except queue.Empty:
                    notification = None
                if notification:
                    import json

                    try:
                        envelope = json.loads(notification)
                    except (ValueError, TypeError):
                        envelope = None
                    if isinstance(envelope, dict) and envelope.get("krest_notice") == 1:
                        self.publish_notice(
                            envelope.get("text", ""),
                            envelope.get("source", "Боты"),
                            envelope.get("priority", "normal"),
                            kind=envelope.get("kind", ""),
                            target=envelope.get("target"),
                        )
                    else:
                        self.publish_notice(notification, "Боты")
                self.performance.tick()
                game = self.performance.game
                self.game_sessions.tick(game)
                if game != getattr(self, "_package_game", ""):
                    self.manager.packages.event(
                        "game.started" if game else "game.stopped",
                        {"game": game or self._package_game},
                        self,
                    )
                    self._package_game = game
                self.audio_focus.tick(bool(self.performance.game))
                if not self.performance.game:
                    for row in self.center.take_after_game():
                        self.publish_notice("После игры: " + row["text"], "Напоминание", "high")
                self.automation.tick()
                self.companion.tick()
                status = {
                    "is_playing": bool(self.performance.game),
                    "game": self.performance.game,
                    "activity_type": "GAME" if self.performance.game else "IDLE",
                    "full_message": "Режим сна" if self.state.is_sleeping else "Ожидание команды",
                    "updated_at": time.time(),
                }
                if (
                    not hasattr(self, "_published")
                    or time.monotonic() - self._published >= 2 * self.performance.poll_multiplier
                ):
                    atomic_write_json(self.status_path, status)
                    self._published = time.monotonic()
                if self.state.shutdown_requested.is_set():
                    self.events.put(("shutdown", None))
                    return
            except Exception as exc:
                logger.exception("Сбой планировщика")
                self.events.put(("error", str(exc)))
                self.stop_event.wait(2)

    def _file_index_worker(self):
        while not self.stop_event.wait(5):
            self.desktop.scan(self.stop_event)
            if self.stop_event.wait(900 if self.performance.game else 300):
                return

    def _clipboard_worker(self):
        while not self.stop_event.wait(1):
            self.clipboard.tick()

    def _catalog_worker(self):
        from app_catalog import scan_apps
        from steam_service import cached_catalog
        import os

        scanned_at = -1e9
        while not self.stop_event.is_set():
            try:
                if os.name == "nt" and time.monotonic() - scanned_at >= 600:
                    scanned_at = time.monotonic()
                    rows = scan_apps()
                    if rows and not self.stop_event.is_set():
                        # Merge with manually added executables; never replace a
                        # concurrently edited settings snapshot with the scan's.
                        with self.config_manager._lock:
                            cfg = self.config_manager.snapshot()
                            manual = [r for r in cfg["applications"] if r["kind"] == "exe"]
                            apps = {r["name"].casefold(): r for r in [*rows, *manual]}
                            cfg["applications"] = sorted(
                                apps.values(), key=lambda r: r["name"].casefold()
                            )
                            if (
                                cfg["applications"]
                                != self.config_manager.snapshot()["applications"]
                            ):
                                self.config_manager.save_config(cfg)
                                self.events.put(("catalog_changed", None))
                cfg = self.settings()
                games = cached_catalog(self.state)
                self.performance.update_games(games)
                self.manager.name_vocabulary.update(
                    cfg.get("applications", []),
                    games,
                    cfg.get("application_aliases", {}),
                )
            except Exception as exc:
                logger.warning("Каталог программ будет обновлён повторно: %s", exc)
            self.stop_event.wait(60)

    def _downloads(self):
        from downloads import DownloadSorter

        sorters = {}
        last_run = 0
        previous = None
        last_error = ""
        was_paused = False
        while not self.stop_event.is_set():
            cfg = self.settings().get("app_settings", {})
            enabled = cfg.get("downloads_auto_sort", False) and self.settings().get(
                "skills", {}
            ).get("system_mon", True)
            policy = self.settings().get("file_policy", {})
            folders = tuple(
                dict.fromkeys([cfg.get("downloads_path", ""), *policy.get("folders", [])])
            )
            signature = (enabled, folders)
            if signature != previous:
                sorters = {folder: DownloadSorter(policy) for folder in folders}
                last_run = 0
                previous = signature
                last_error = ""
            if enabled:
                # Recheck even when an unrelated scheduler task is slow. The
                # shared detector scans processes at most once per five seconds.
                self.performance.refresh_activity()
            pause_reason = self.performance.file_pause_reason if enabled else ""
            if pause_reason:
                was_paused = True
                self.download_status = "Сортировка приостановлена: " + pause_reason
            elif enabled and (was_paused or time.monotonic() - last_run >= 30):
                was_paused = False
                last_run = time.monotonic()
                self.download_status = "Проверяю папку загрузок…"
                try:
                    result = {"moved_count": 0, "skipped_count": 0, "errors": []}
                    for folder in folders:
                        sorter = sorters[folder]
                        sorter.policy = policy
                        try:
                            item = sorter.scan(folder, stop=self.stop_event)
                            result["moved_count"] += item["moved_count"]
                            result["skipped_count"] += item["skipped_count"]
                            result["errors"].extend(item["errors"])
                        except (OSError, ValueError) as exc:
                            result["errors"].append(str(exc))
                    self.download_status = f"Проверено в {time.strftime('%H:%M:%S')} · перемещено: {result['moved_count']} · ожидают: {result['skipped_count']}"
                    if result["moved_count"]:
                        self.publish_notice("Загрузки: " + self.download_status, "Файлы", "low")
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
                was_paused = False
                self.download_status = "Автосортировка выключена"
            self.stop_event.wait(1)

    def publish_notice(
        self, text, source="Система", priority="normal", speak=True, kind="", target=None
    ):
        if priority not in ("low", "normal", "high"):
            priority = "normal"
        ident, fresh = self.center.add(text, source, priority, kind=kind, target=target)
        if isinstance(target, dict) and self.center.muted(source, target.get("chat", "")):
            return ident
        cfg = self.settings().get("notification_settings", {})
        mode = self.notification_mode or cfg.get("mode", "all")
        if (
            fresh
            and speak
            and (kind != "message" or cfg.get("announce_senders", True))
            and should_speak(
                priority,
                mode,
                self.performance.active == "game"
                and not (kind == "message" and cfg.get("announce_senders", True)),
                cfg.get("game_important_only", True),
            )
        ):
            voice.speak(text)
        if fresh:
            self.events.put(("notification", str(text)))
        return ident

    def is_scenario_command(self, text):
        from speech_tools import normalize_command

        wake, command = check_wake_word(
            text, self.settings().get("app_settings", {}).get("wake_word", "крест")
        )
        matched = self.manager.match(
            normalize_command(command if wake else text, self.settings().get("command_aliases", {}))
        )
        return matched and matched[0].func.__name__ in (
            "run_scenario",
            "stop_scenario",
            "create_scenario",
            "save_scenario_draft",
            "cancel_scenario_draft",
        )

    def start_voice(self):
        if self.stop_event.is_set():
            return
        if self._voice_thread and self._voice_thread.is_alive():
            return
        self._voice_stop.clear()
        self._voice_thread = threading.Thread(
            target=self._voice_loop, daemon=True, name="krest-listen"
        )
        self._voice_thread.start()

    def _voice_loop(self):
        from audio_scheduling import recognition_priority

        with recognition_priority():
            self._voice_loop_scheduled()

    def _voice_loop_scheduled(self):
        failures = 0
        while not self.stop_event.is_set() and not self._voice_stop.is_set():
            started = time.monotonic()
            failure = self._voice_attempt()
            if not failure:
                return
            if time.monotonic() - started > 60:
                failures = 0
            delay = (1, 3, 10, 30)[min(failures, 3)]
            failures += 1
            self.voice_status = failure + f" · Повторю подключение через {delay} с"
            self.events.put(("voice", self.voice_status))
            for _ in range(delay):
                if self.stop_event.is_set() or self._voice_stop.wait(1):
                    return

    def _voice_attempt(self):
        failure = ""
        tts_stop = threading.Event()
        self.voice_status = "Подключаю голос: ищу модели и микрофон…"
        self.events.put(("voice", self.voice_status))
        try:
            from model_setup import prepare

            def report(text):
                self.voice_status = text
                self.events.put(("voice", text))

            prepare(
                self.settings(),
                report,
                lambda: self.stop_event.is_set() or self._voice_stop.is_set(),
                components=("vosk_path",),
            )
            if self.stop_event.is_set() or self._voice_stop.is_set():
                return
            with self._voice_control:
                voice.enable()
            # Catalog/SQLite reads must never pause the audio consumer.
            threading.Thread(
                target=self._voice_dictionary,
                args=(tts_stop,),
                daemon=True,
                name="krest-voice-dictionary",
            ).start()
            if not voice.telemetry().get("tts_ready"):
                threading.Thread(
                    target=self._tts_recovery,
                    args=(tts_stop,),
                    daemon=True,
                    name="krest-voice-recovery",
                ).start()
            connected = bool(voice.telemetry().get("connected"))
            self.voice_status = (
                "Слушаю обращение «Крест»" if connected else "Ищу доступный микрофон…"
            )
            self.events.put(
                ("voice", "Микрофон активен" if connected else "Ищу доступный микрофон…")
            )
            from speech_turn import VoiceTurn

            turn = VoiceTurn(self.manager, self.feedback)
            while not self.stop_event.is_set() and not self._voice_stop.is_set():
                voice.wait_for_audio(0.03)
                if self._arm_requested.is_set():
                    self._arm_requested.clear()
                    self._active_until = time.monotonic() + self.settings().get(
                        "app_settings", {}
                    ).get("active_timeout", 6)
                    turn.clear()
                    waiting = self.feedback.begin("listening", "Слушаю команду")
                    self.feedback.update(
                        waiting,
                        "listening",
                        "Слушаю команду",
                        max(0, self._active_until - time.monotonic()),
                    )
                    self.events.put(("voice", "Слушаю команду без обращения"))
                text = voice.listen()
                telemetry = voice.telemetry()
                now_connected = bool(telemetry.get("connected"))
                if telemetry.get("recovery") or not now_connected:
                    turn.clear(telemetry.get("recovery") or "Микрофон переподключается")
                else:
                    turn.observe(telemetry.get("partial", ""))
                if now_connected != connected:
                    connected = now_connected
                    self.voice_status = (
                        "Слушаю обращение «Крест»" if connected else "Переподключаю микрофон…"
                    )
                    self.events.put(
                        (
                            "voice",
                            "Подключён микрофон: " + voice.telemetry().get("device_name", "")
                            if connected
                            else "Микрофон отключён, пробую подключиться снова…",
                        )
                    )
                recognized = turn.push(text, voice.recognition()) if text else turn.flush()
                for text, recognition, feedback_turn in recognized:
                    self._dispatch_voice(text, recognition, feedback_turn)
        except Exception as exc:
            failure = str(exc)
            self.events.put(("error", f"Голос: {exc}"))
        finally:
            tts_stop.set()
            with self._voice_control:
                voice.disable()
            self.voice_status = failure or "Микрофон на паузе"
            self.events.put(("voice", self.voice_status))
        return failure

    def _dispatch_voice(self, text, recognition, feedback_turn=None):
        """Accept final audio on capture time; a wake word never waits in a job lane."""
        from speech_recognition import decode_voice_command, command_confidence

        if self.hearing.consume(text, recognition):
            # Includes background chat: no job, player, microphone or screenshot side effect.
            self._active_until = 0
            if feedback_turn is not None:
                self.feedback.update(
                    feedback_turn, "listening", "Проверка слуха — действие не выполняется", 3
                )
            return True
        settings = self.settings()
        cfg = settings.get("app_settings", {})
        addressed, command = decode_voice_command(self.manager, text, settings)
        recognition = dict(recognition)
        recognition.pop("listening_window", None)
        if addressed:
            voice.stop_speaking()
            if not command:
                score = command_confidence(
                    self.manager,
                    text,
                    command,
                    True,
                    recognition.get("confidence"),
                    recognition,
                    cfg,
                )
                if score is not None and score * 100 < cfg.get("speech_confidence", 60):
                    if feedback_turn is not None:
                        self.feedback.update(feedback_turn, "error", "Не расслышала обращение", 3)
                    self._voice_retry(True)
                    return False
                self._active_until = time.monotonic() + cfg.get("active_timeout", 6)
                turn = feedback_turn if feedback_turn is not None else self.feedback.begin()
                self.feedback.update(
                    turn, "listening", "Слушаю команду", cfg.get("active_timeout", 6)
                )
                self.events.put(
                    (
                        "recognition",
                        {
                            "heard": text,
                            "command": "",
                            "confidence": score,
                            "status": "Ожидаю команду",
                        },
                    )
                )
                from companion_behavior import policy

                voice.speak(policy(settings, bool(self.performance.game))["listen"])
                return True
        elif time.monotonic() > self._active_until:
            # Game chat never enters the job queue or touches its SQLite journal.
            self.events.put(
                (
                    "recognition",
                    {
                        "heard": text,
                        "command": "",
                        "confidence": recognition.get("confidence"),
                        "status": "Без обращения — команда не выполнена",
                    },
                )
            )
            return False
        else:
            recognition["listening_window"] = True
            self._active_until = 0
        return self.submit(
            text,
            "voice",
            recognition.get("confidence"),
            recognition=recognition,
            feedback_turn=feedback_turn,
        )

    def _voice_dictionary(self, stopped):
        from speech_dictionary import quick_phrases

        while not stopped.is_set() and not self.stop_event.is_set():
            try:
                cfg = self.settings()
                names = self.manager.name_vocabulary.phrases()
                phrases = quick_phrases(self.manager, cfg, self.companion.snapshot()["learning"])
                phrases |= names | {cfg["app_settings"]["wake_word"] + " " + p for p in names}
                if stopped.is_set():
                    return
                voice.set_command_dictionary(phrases)
                voice.refresh_output()
            except Exception:
                logger.exception("Не удалось обновить голосовой словарь")
            if stopped.wait(5):
                return

    def _tts_recovery(self, stopped):
        from model_setup import prepare

        def cancelled():
            return stopped.is_set() or self.stop_event.is_set() or self._voice_stop.is_set()

        delay = 60
        while not cancelled():
            try:
                voice.tts_progress("Подключаю голос Silero…")
                models = prepare(
                    self.settings(),
                    voice.tts_progress,
                    cancelled,
                    components=("silero_path",),
                )
                if cancelled():
                    return
                if models["silero_path"] and voice.refresh_tts(models, cancelled):
                    self.events.put(("voice", "Голос Silero подключён"))
                    return
                if not self.settings().get("app_settings", {}).get("voice_auto_models", True):
                    voice.tts_progress(
                        "Автозагрузка моделей выключена. Добавьте Silero в папку model."
                    )
                else:
                    detail = voice.telemetry().get("tts_progress", "")
                    voice.tts_progress(
                        detail.split(" · Повторю")[0] + f" · Повторю через {delay} секунд"
                    )
            except InterruptedError:
                return
            except Exception as exc:
                voice.tts_progress(f"Silero: {str(exc)[:200]} · Повторю через {delay} секунд")
            for _ in range(delay):
                if cancelled() or stopped.wait(1):
                    return
            delay = min(300, delay * 2)

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
        self.verifier.closed.set()
        self.audio_focus.close()
        with self._request_lock:
            self.stop_event.set()
            for ready in self._queue_ready.values():
                ready.set()
            self.clear_pending()
        self.warframe.close()
        self.scenarios.cancel()
        if self.scenarios.thread:
            self.scenarios.thread.join(timeout=1)
        self.stop_voice()
        for thread in self._threads:
            thread.join(timeout=1)
        self.game_sessions.close()
        self.state.persist()
