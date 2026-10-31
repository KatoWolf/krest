"""Authenticated Dota/CS2 match signals; process presence alone is not a match."""

import hashlib
import hmac
from pathlib import Path
import re
import secrets
import threading
import time
from storage import read_json, atomic_write_json

GAMES = {
    "570": (
        "Dota 2",
        Path("game/dota/cfg/gamestate_integration/gamestate_integration_krest_session.cfg"),
        {"dota2.exe"},
    ),
    "730": ("CS2", Path("game/csgo/cfg/gamestate_integration_krest_session.cfg"), {"cs2.exe"}),
}


class GamePhase:
    def __init__(self, runtime, root, clock=time.monotonic):
        self.runtime, self.clock = runtime, clock
        self.path = Path(root) / "game_phase.json"
        try:
            self.state = read_json(self.path)
            if (
                not isinstance(self.state, dict)
                or not isinstance(self.state.get("files", {}), dict)
                or not isinstance(self.state.get("token", ""), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,100}", self.state.get("token", ""))
            ):
                raise ValueError("Некорректное состояние датчика")
            for appid, record in self.state.get("files", {}).items():
                if (
                    appid not in GAMES
                    or not isinstance(record, dict)
                    or not isinstance(record.get("path"), str)
                    or not isinstance(record.get("hash"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record["hash"])
                ):
                    raise ValueError("Некорректный путь датчика")
        except (OSError, ValueError, TypeError):
            self.state = {}
        self.state.setdefault("token", secrets.token_urlsafe(32))
        self.state.setdefault("files", {})
        self.lock = threading.RLock()
        self.url = ""
        self.received = 0
        self.active = False
        self.game = ""
        self.manual = None
        self.manual_until = 0
        self.manual_game = ""
        self.hold_off = False
        self.last_packet = -1e9
        self.message = ""

    def save(self):
        atomic_write_json(self.path, self.state)

    def configure_endpoint(self, port):
        self.url = f"http://127.0.0.1:{port}/game-state"
        for appid, record in list(self.state["files"].items()):
            try:
                path = self.config_path(appid, record["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest() != record["hash"]:
                    self.message = "Файл датчика изменён вручную; подключите его повторно в меню"
                    continue
                self._write(appid, path)
            except (OSError, ValueError, TypeError, KeyError):
                self.message = "Файл датчика игры недоступен; проверьте подключение в меню"

    @staticmethod
    def config_path(appid, path):
        from autonomy_files import clean_path

        path = clean_path(Path(path))
        if appid not in GAMES or path.parts[-len(GAMES[appid][1].parts) :] != GAMES[appid][1].parts:
            raise ValueError("Неожиданный путь датчика игры")
        if path.exists() and (not path.is_file() or path.stat().st_size > 16384):
            raise ValueError("Некорректный файл датчика игры")
        return path

    def _write(self, appid, path):
        path = self.config_path(appid, path)
        if appid not in GAMES or not self.url:
            raise ValueError("Запустите Крест штатным ярлыком, чтобы подключить датчик игры")
        text = '"Krest Session Guard"\n{\n'
        text += f' "uri" "{self.url}"\n "timeout" "1.0"\n "buffer" "0.5"\n "throttle" "2.0"\n "heartbeat" "5.0"\n'
        text += f' "auth" {{ "token" "{self.state["token"]}" }}\n'
        text += ' "data" { "provider" "1" "map" "1" "player_id" "1" }\n}\n'
        path.parent.mkdir(parents=True, exist_ok=True)
        # The unique file belongs to Krest; never edit the game's other configs.
        from storage import atomic_write_text

        atomic_write_text(path, text)
        self.state["files"][appid] = {
            "path": str(path),
            "hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        self.save()

    def install(self, appid):
        if appid not in GAMES:
            raise ValueError("Поддерживаются датчики Dota 2 и CS2")
        from steam_service import local_games
        from autonomy_files import clean_path

        games = local_games(self.runtime.settings().get("steam", {}).get("install_path", ""))
        game = next((r for r in games if str(r["appid"]) == appid), None)
        if not game:
            raise ValueError("Игра не найдена в локальных библиотеках Steam")
        common = clean_path(Path(game["library"]) / "steamapps" / "common")
        root = clean_path(common / game["install_dir"])
        if root == common or not root.is_relative_to(common):
            raise ValueError("Папка игры находится за пределами библиотеки Steam")
        path = self.config_path(appid, root / GAMES[appid][1])
        if not (root / "game" / ("dota" if appid == "570" else "csgo") / "cfg").is_dir():
            raise ValueError("Не найдена папка настроек игры")
        if path.exists():
            owned = self.state["files"].get(appid, {})
            if (
                owned.get("path") != str(path)
                or owned.get("hash") != hashlib.sha256(path.read_bytes()).hexdigest()
            ):
                raise ValueError(
                    "Файл с таким именем уже существует или изменён. Он не перезаписан."
                )
        self._write(appid, path)
        self.message = ""
        return (
            GAMES[appid][0]
            + ": датчик установлен. Перезапустите игру."
            + (
                " В параметрах запуска Steam нужен -gamestateintegration; кнопка запуска здесь передаст его на один запуск."
                if appid == "570"
                else ""
            )
        )

    def receive(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("auth"), dict):
            return False
        token = data["auth"].get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, self.state["token"]):
            return False
        if not self.runtime.settings().get("download_guard", {}).get(
            "gsi"
        ) or self.runtime.settings().get("safe_mode"):
            return True
        provider, game = data.get("provider", {}), data.get("map", {})
        if not isinstance(provider, dict) or not isinstance(game, dict):
            return True
        appid = str(provider.get("appid", ""))
        if appid not in GAMES or appid not in self.state["files"]:
            return True
        phase = game.get("game_state", "") if appid == "570" else game.get("phase", "")
        live = (
            phase in ("DOTA_GAMERULES_STATE_GAME_IN_PROGRESS", "DOTA_GAMERULES_STATE_PRE_GAME")
            if appid == "570"
            else phase == "live"
        )
        # Spectating does not need an automatic playing-session restriction.
        if (
            data.get("player", {}).get("activity") == "menu"
            if isinstance(data.get("player", {}), dict)
            else False
        ):
            live = False
        with self.lock:
            if self.clock() - self.last_packet < 1:
                return True
            self.last_packet = self.clock()
            self.received = self.clock()
            self.active = live
            self.game = appid
            if not live:
                self.hold_off = False
        return True

    def set_manual(self, active):
        with self.lock:
            self.manual = True if active else None
            self.manual_until = self.clock() + 4 * 3600
            self.manual_game = self.runtime.performance.game
            self.hold_off = not active
        return (
            "Матч отмечен: защита включена для выбранного загрузчика"
            if active
            else "Матч завершён: возвращаю прежние ограничения загрузчика"
        )

    def snapshot(self):
        with self.lock:
            now = self.clock()
            process = self.runtime.performance.game_process.casefold()
            if self.manual is not None and (
                now >= self.manual_until or self.manual_game and not self.runtime.performance.game
            ):
                self.manual = None
            if self.manual is not None:
                active = self.manual
                text = "Матч отмечен вашей командой" if active else "Матч завершён вашей командой"
            else:
                alive = bool(self.game in GAMES and process in GAMES[self.game][2])
                fresh = bool(self.received and 0 <= now - self.received < 30)
                # Missing packets are unknown, not proof that a match ended.
                held = bool(alive and self.active and 30 <= now - self.received < 120)
                active = bool(
                    alive
                    and (fresh or held)
                    and self.active
                    and not self.hold_off
                    and self.runtime.settings().get("download_guard", {}).get("gsi")
                )
                text = (
                    (
                        "Матч подтверждён игрой"
                        if fresh
                        else "Сигнал временно потерян; сохраняю защиту до двух минут"
                    )
                    if active
                    else "Матч не подтверждён; сам запуск игры загрузки не блокирует"
                )
            return {
                "active": active,
                "text": text,
                "source": "manual" if self.manual is not None else "game",
                "connected": list(self.state["files"]),
                "setup_message": self.message,
            }
