"""Shared experience state owned by the kernel: learning, drafts and diagnostics."""

import copy
import threading
import time
from speech_learning import SpeechLearning, profile_key


class CompanionSession:
    def __init__(self, runtime, root):
        self.runtime = runtime
        self.learning = SpeechLearning(root / "speech_learning.sqlite3")
        self.lock = threading.RLock()
        self.topic = ""
        self.topic_until = 0
        self.last_voice = ""
        self.last_voice_at = 0
        self.last_trace = {}
        self.draft = None
        self._learning_rows = []
        self._learning_at = 0
        self._learning_profile = ""
        self.legacy_profile = self.profile
        self._initiative_check = 0

    def tick(self):
        from companion_behavior import policy
        from datetime import date

        now = time.monotonic()
        if now - self._initiative_check < 300:
            return
        self._initiative_check = now
        if not policy(self.runtime.settings(), bool(self.runtime.performance.game))["initiative"]:
            return
        key = "привычки предложены " + self.profile
        today = date.today().isoformat()
        if self.runtime.memory.recall(key) == today:
            return
        from assistant_memory import routine_suggestions

        rows = routine_suggestions(self.runtime.jobs.list(2000))
        if rows:
            self.runtime.publish_notice(
                "Повторяется в разные дни: «"
                + rows[0]["command"]
                + "». Можно сохранить в сценарий; команда «предложи привычки» покажет детали.",
                "Привычки",
                speak=False,
            )
            self.runtime.memory.remember(key, today, "preference")

    @property
    def profile(self):
        return profile_key(self.runtime.settings())

    def snapshot(self):
        with self.lock:
            profile = self.profile
            if profile != self._learning_profile or time.monotonic() - self._learning_at >= 2:
                self._learning_profile = profile
                self._learning_rows = self.learning.list(profile)
                self._learning_at = time.monotonic()
            return {
                "topic": self.topic if time.monotonic() < self.topic_until else "",
                "remaining": max(0, round(self.topic_until - time.monotonic())),
                "trace": dict(self.last_trace),
                "draft": copy.deepcopy(self.draft)
                if self.draft and self.draft["profile"] == profile
                else None,
                "learning": copy.deepcopy(self._learning_rows),
            }

    def remember_topic(self, skill):
        domain = next(
            (
                label
                for prefix, label in (
                    ("skills.media.", "Музыка"),
                    ("skills.discord_voice.", "Микрофон Discord"),
                    ("skills.timers_alarms.", "Таймеры"),
                    ("skills.warframe.", "Warframe"),
                )
                if skill.startswith(prefix)
            ),
            "",
        )
        with self.lock:
            self.topic = domain
            self.topic_until = time.monotonic() + self.runtime.settings().get(
                "app_settings", {}
            ).get("active_timeout", 6)

    def accepts_followup(self, command):
        if not self.topic or time.monotonic() >= self.topic_until:
            return True
        if command.casefold().strip(" ,.!?") in (
            "да",
            "нет",
            "отмена",
            "первое",
            "первый",
            "второй",
            "верно",
            "правильно",
            "подтверждаю",
            "не надо",
            "один",
            "два",
            "1",
            "2",
            "второе",
            "верно",
            "правильно",
            "подтверждаю",
        ):
            return True
        matched = self.runtime.manager.match(command)
        if self.topic in ("Музыка", "Микрофон Discord"):
            return bool(
                matched and matched[0].id.startswith(("skills.media.", "skills.discord_voice."))
            )
        return True

    def record_correction(self, heard, choice):
        row = self.learning.record(self.profile, heard, choice["command"], choice["skill"])
        self._learning_at = 0
        if row and row["hits"] == 2 and row["status"] == "pending":
            self.runtime.publish_notice(
                f"Повторяющееся исправление: «{heard}» → «{choice['command']}». В разделе «Голос → Обучение» можно подтвердить запоминание.",
                "Обучение речи",
                speak=False,
            )
        return row

    def call(self, action, **args):
        if action == "snapshot":
            return self.snapshot()
        if action == "learning.decide":
            self._learning_at = 0
            return self.learning.decide(self.profile, str(args["heard"]), args["status"])
        if action == "draft.create":
            from scenario_draft import parse_draft

            draft = parse_draft(args["text"], self.runtime.manager)
            with self.lock:
                self.draft = dict(draft, profile=self.profile)
            return (
                draft["text"]
                + "\nСкажите «сохрани сценарий» или «отмени черновик». Действия пока не запускались."
            )
        if action == "draft.cancel":
            with self.lock:
                self.draft = None
            return "Черновик отменён."
        if action == "draft.save":
            with self.lock:
                if not self.draft or self.draft["profile"] != self.profile:
                    raise ValueError("Нет черновика этого профиля")
                draft = copy.deepcopy(self.draft)
                manager = self.runtime.config_manager
                cfg = manager.snapshot()
                if any(n.casefold() == draft["name"].casefold() for n in cfg["scenarios"]):
                    raise ValueError(
                        "Такой сценарий уже есть. Дайте черновику другое имя или измените сценарий в меню."
                    )
                cfg["scenarios"][draft["name"]] = draft["steps"]
                manager.save_config(cfg)
                self.draft = None
            self.runtime.events.put(("scenario_saved", draft["name"]))
            return (
                "Сценарий «"
                + draft["name"]
                + "» сохранён. Скажите «запусти сценарий "
                + draft["name"]
                + "»."
            )
        if action == "preference.set":
            key, value = args["key"], str(args["value"]).strip()
            if (
                key not in ("любимый плеер", "любимая игра", "время напоминаний")
                or not 1 <= len(value) <= 150
            ):
                raise ValueError(
                    "Предпочтение: плеер, игра или время напоминаний (до 150 символов)"
                )
            if key == "время напоминаний":
                from skills.timers_alarms import parse_alarm_time

                if parse_alarm_time(value) is None:
                    raise ValueError("Назовите время, например девять утра или 09:00")
            self.runtime.memory.remember(key, value, "preference")
            return "Запомнила: " + key + " — " + value + "."
        raise ValueError("Неизвестное действие помощника")
