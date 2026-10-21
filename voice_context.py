"""Small, expiring dialogue state. No fuzzy matching or background inference."""

import re
import threading
import time


class VoiceContext:
    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.RLock()
        self.target = {}
        self.until = 0.0
        self.generation = 0
        self.profile = ""

    def clear(self):
        with self.lock:
            self.target = {}
            self.until = 0
            self.generation += 1

    def _profile(self):
        from speech_learning import profile_key

        return profile_key(self.settings())

    def remember(self, skill, command, value=None, previous=None, revision=None):
        cfg = self.settings().get("app_settings", {})
        target = {}
        name = skill.rsplit(".", 1)[-1]
        if skill.startswith("skills.media."):
            if name in {
                "volume_up",
                "volume_down",
                "set_exact_volume",
                "set_volume_max",
                "mute_system",
                "unmute_system",
            }:
                target = dict(previous or {"scope": "volume"})
                target["direction"] = {"volume_up": 1, "volume_down": -1}.get(name, 0)
            elif name in {
                "next_track",
                "prev_track",
                "pause_music",
                "play_music",
                "toggle_play_pause",
                "like_track",
                "unlike_track",
                "dislike_track",
            }:
                target = {"scope": "music", **(previous or {})}
        if isinstance(value, dict) and isinstance(value.get("voice_context"), dict):
            target.update(value["voice_context"])
        if target.get("scope") == "music" and not any(
            target.get(k) for k in ("player", "process", "name")
        ):
            target = {}
        if skill.startswith("skills.discord_voice."):
            target = {"scope": "discord"}
        if skill == "skills.workflows.app_volume":
            match = re.fullmatch(r"громкость приложения (.+) на \d{1,3}(?: процентов)?", command)
            if match:
                target = {"scope": "volume", "process": match[1]}
        with self.lock:
            if revision is not None and revision != self.generation:
                return  # A later user command superseded this slow completion.
            self.clear()
            if target and cfg.get("voice_context", True):
                self.target = target
                self.profile = self._profile()
                self.until = time.monotonic() + cfg.get("voice_context_seconds", 10)

    def snapshot(self):
        with self.lock:
            if (
                time.monotonic() >= self.until
                or self.profile != self._profile()
                or not self.settings().get("app_settings", {}).get("voice_context", True)
            ):
                return {}
            return {
                "target": dict(self.target),
                "remaining": round(self.until - time.monotonic(), 1),
                "generation": self.generation,
            }

    def resolve(self, phrase):
        current = self.snapshot()
        if not current:
            return None
        clean = " ".join(phrase.casefold().replace("ё", "е").strip(" ,.!?").split())
        if clean in {"хватит", "все", "отмена", "не надо"}:
            self.clear()
            return None
        target = current["target"]
        scope = target.get("scope")
        commands = {}
        if scope in {"music", "volume"}:
            for phrases, command in (
                (("сделай потише", "потише", "сделай тише", "тише", "еще тише"), "тише"),
                (
                    ("сделай погромче", "погромче", "сделай громче", "громче", "еще громче"),
                    "громче",
                ),
            ):
                commands.update(dict.fromkeys(phrases, command))
            if target.get("direction") in (-1, 1):
                commands.update(
                    dict.fromkeys(
                        ("еще немного", "еще чуть-чуть", "еще чуть чуть", "еще"),
                        "тише на пять" if target["direction"] == -1 else "громче на пять",
                    )
                )
        if scope == "music":
            commands.update(
                {
                    "следующий трек": "следующий трек",
                    "переключи трек": "следующий трек",
                    "предыдущий трек": "предыдущий трек",
                    "поставь на паузу": "пауза",
                    "продолжи музыку": "продолжи музыку",
                    "включи обратно": "продолжи музыку",
                    "поставь лайк": "поставь лайк",
                    "убери лайк": "убери лайк",
                }
            )
        if scope == "discord":
            commands.update(
                {
                    "включи обратно": "включи микро",
                    "включи микро": "включи микро",
                    "выключи микро": "выключи микро",
                }
            )
        command = commands.get(clean)
        return dict(current, command=command, heard=phrase) if command else None

    def valid(self, token):
        current = self.snapshot()
        resolved = self.resolve(token.get("heard", "")) if isinstance(token, dict) else None
        return bool(
            current
            and resolved
            and token.get("generation") == current["generation"]
            and token.get("command") == resolved["command"]
            and token.get("target") == current["target"]
        )
