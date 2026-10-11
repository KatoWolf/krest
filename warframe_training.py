"""Local, opt-in Apollo demonstrations and reports. Never injects game input."""

import copy
from collections import Counter
import re
import shutil
from pathlib import Path
import threading
import time
import uuid

from storage import atomic_write_json, read_json

PROFILE = {
    "id": "lua-apollo-axi",
    "node": "Аполлон · Луа · Сбой",
    "target": "Реликвии Акси",
    "warframe": "Валькирия Прайм",
    "source": "Профиль выбран пользователем; узел и экипировка в игре не проверены",
    "autonomous": False,
}
LABELS = {
    "ключ": "Нашёл ключ",
    "проводник": "Активировал проводник",
    "демолист": "Обнаружил демолиста",
    "защита": "Защитил проводник",
    "провал": "Потерял проводник",
    "награда": "Получил награду",
    "эвакуация": "Начал эвакуацию",
}
MAX_SECONDS = 20 * 60
MAX_FRAMES = 240
MAX_EVENTS = 16000
MAX_FRAME_BYTES = 1024 * 1024
MAX_STORAGE_BYTES = 512 * 1024**2
MAX_SESSIONS = 100
FRAME_INTERVAL = 5


def summarize(data):
    """Describe measured examples; observations never grant execution capability."""
    labels, controls, screens = Counter(), Counter(), Counter()
    previous = set()
    for event in data["events"]:
        if event["kind"] == "label":
            labels[event["label"]] += 1
        elif event["kind"] == "controls":
            keys = set(event["keys"])
            controls.update(keys - previous)
            previous = keys
        elif event["kind"] == "focus_lost":
            previous = set()
        elif event["kind"] == "screen":
            screens[event["screen"]] += 1
    return {
        "frames": len(data["frames"]),
        "seconds": int(data["duration"]),
        "labels": dict(labels),
        "controls": dict(controls),
        "screens": dict(screens),
    }


class WindowsStudyPort:
    def __init__(self):
        from game_routes import WindowsInput

        self.input = WindowsInput()

    def sample(self):
        from game_capture import foreground_game_rect

        bounds = foreground_game_rect()
        keys = self.input.pressed()
        for key, code in {
            "ability1": 0x31,
            "ability2": 0x32,
            "ability3": 0x33,
            "ability4": 0x34,
            "fire": 0x01,
            "aim": 0x02,
            "melee": 0x45,
            "interact": 0x58,
        }.items():
            if self.input.user.GetAsyncKeyState(code) & 0x8000:
                keys.add(key)
        if foreground_game_rect() != bounds:
            raise ValueError("Окно игры изменилось")
        return sorted(keys)

    def capture(self, path):
        from game_capture import foreground_game_rect
        from PIL import ImageGrab

        bounds = foreground_game_rect()
        image = ImageGrab.grab(bbox=bounds, all_screens=True)
        try:
            if foreground_game_rect() != bounds:
                raise ValueError("Окно игры изменилось; кадр отброшен")
            source_size = image.size
            image.thumbnail((1024, 576))
            rgb = image.convert("RGB")
            try:
                rgb.save(path, format="JPEG", quality=70)
            finally:
                rgb.close()
            if foreground_game_rect() != bounds:
                raise ValueError("Окно игры изменилось; кадр отброшен")
            return {"source_size": list(source_size), "image_size": list(image.size)}
        finally:
            image.close()


class MissionStudy:
    """Collect sparse frames, control states and explicit labels, not a learned policy."""

    def __init__(self, root, allowed=lambda: True, port_factory=WindowsStudyPort):
        self.root = Path(root) / "warframe_training"
        self.allowed = allowed
        self.port_factory = port_factory
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.thread = None
        self.data = None
        self.folder = None
        self.message = "Показов ещё нет · автономное прохождение не обучено"
        self.active = False
        self.automatic = False
        self.next_segment = False
        self.started = 0
        self._seen_observation = 0
        self._candidate = ""
        self._confirmed = ""
        self.history = {}
        self.summary = {}
        self.history_error = ""
        history_path = self.root / "learning.json"
        if history_path.exists():
            try:
                if history_path.stat().st_size > 256000:
                    raise ValueError("Слишком большой отчёт обучения")
                history = read_json(history_path)
                if not isinstance(history, dict) or len(history) > MAX_SESSIONS:
                    raise ValueError("Повреждён отчёт обучения")
                for row in history.values():
                    if not isinstance(row, dict):
                        raise ValueError("Повреждён отчёт обучения")
                    for key in ("frames", "seconds"):
                        if type(row.get(key)) is not int or not 0 <= row[key] <= MAX_EVENTS:
                            raise ValueError("Повреждён счётчик обучения")
                    for key in ("labels", "controls", "screens"):
                        values = row.get(key)
                        if (
                            not isinstance(values, dict)
                            or len(values) > 64
                            or any(
                                not isinstance(k, str)
                                or len(k) > 80
                                or type(v) is not int
                                or not 0 <= v <= MAX_EVENTS
                                for k, v in values.items()
                            )
                        ):
                            raise ValueError("Повреждена статистика обучения")
                self.history = history
            except (OSError, ValueError, TypeError) as exc:
                self.history_error = str(exc)
        if self.history:
            self.message = "Опыт сохранён · ожидаю активное окно Warframe"

    def _make_room(self):
        """Rotate only our automatic sessions, never user-added files or manual shows."""
        folders = sorted(p for p in self.root.glob("*") if p.is_dir() and not p.is_symlink())
        used = sum(
            p.stat().st_size
            for folder in folders
            for p in folder.iterdir()
            if p.is_file() and not p.is_symlink()
        )
        count = sum((folder / "manifest.json").is_file() for folder in folders)
        for folder in folders:
            if (
                count < MAX_SESSIONS
                and used + (MAX_FRAMES + 8) * MAX_FRAME_BYTES <= MAX_STORAGE_BYTES
            ):
                return
            path = folder / "manifest.json"
            children = list(folder.iterdir())
            if (
                not path.is_file()
                or path.stat().st_size > 4 * 1024**2
                or any(
                    p.is_symlink()
                    or not p.is_file()
                    or not (
                        p.name in ("manifest.json", "report.txt")
                        or re.fullmatch(r"frame-\d{4}\.(jpg|partial)", p.name)
                    )
                    for p in children
                )
            ):
                continue
            try:
                data = read_json(path)
                if (
                    data.get("version") != 1
                    or data.get("automatic") is not True
                    or data.get("id") != folder.name
                ):
                    continue
                size = sum(p.stat().st_size for p in children)
                shutil.rmtree(folder)
                used -= size
                count -= 1
            except (OSError, ValueError, TypeError, AttributeError):
                continue
        if count >= MAX_SESSIONS or used + (MAX_FRAMES + 8) * MAX_FRAME_BYTES > MAX_STORAGE_BYTES:
            raise ValueError(
                "Папка показов заполнена. Автоматические записи очищены; перенесите старые ручные записи."
            )

    def start(self, automatic=False):
        with self.lock:
            if self.thread and self.thread.is_alive():
                raise ValueError("Показ уже записывается. Скажите «закончи показ варфрейм».")
            if not self.allowed():
                raise ValueError("Включите помощника Warframe и выйдите из безопасного режима")
            if self.history_error:
                raise ValueError(
                    self.history_error
                    + ". Перенесите learning.json из папки показов для восстановления отчёта."
                )
            port = self.port_factory()
            self._make_room()
            ident = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            self.folder = self.root / ident
            self.folder.mkdir(parents=True)
            self.started = time.monotonic()
            self.data = {
                "version": 1,
                "id": ident,
                "profile": copy.deepcopy(PROFILE),
                "started": time.time(),
                "duration": 0,
                "state": "recording",
                "automatic": bool(automatic),
                "frames": [],
                "events": [],
                "limitations": [
                    "Данные показов; модель управления не обучена",
                    "Клавиши по стандартной раскладке; направление камеры не записывается",
                    "Голосовые метки — слова пользователя; количество добычи не проверено",
                    "Кадры содержат видимый интерфейс игры и могут содержать чат",
                ],
            }
            self._seen_observation = 0
            self._candidate = self._confirmed = ""
            self.message = "Ожидаю активное окно Warframe"
            self.stop.clear()
            self._save()
            self.automatic = bool(automatic)
            self.next_segment = False
            self.active = True
            self.thread = threading.Thread(
                target=self._run, args=(port,), daemon=True, name="krest-warframe-study"
            )
            self.thread.start()
        return "Запись показа включена на 20 минут: кадр раз в 5 секунд и клавиши активного Warframe. Управление у вас. Отмечайте события: «варфрейм метка ключ», «варфрейм метка демолист». «Закончи показ варфрейм» завершит запись."

    def _event(self, kind, **values):
        if len(self.data["events"]) >= MAX_EVENTS:
            raise ValueError("Достигнут предел событий показа")
        event = {"at": round(time.monotonic() - self.started, 3), "kind": kind, **values}
        self.data["events"].append(event)
        return event

    def _save(self):
        self.data["duration"] = round(time.monotonic() - self.started, 1)
        atomic_write_json(self.folder / "manifest.json", self.data)
        self.summary = summarize(self.data)
        self.history[self.data["id"]] = self.summary
        self.history = dict(list(self.history.items())[-MAX_SESSIONS:])
        atomic_write_json(self.root / "learning.json", self.history)

    def _frame(self, port):
        index = len(self.data["frames"])
        path = self.folder / f"frame-{index:04d}.jpg"
        pending = path.with_suffix(".partial")
        try:
            details = port.capture(pending)
            if pending.stat().st_size > MAX_FRAME_BYTES:
                raise RuntimeError("Кадр слишком большой; запись остановлена")
            with self.lock:
                if self.stop.is_set() or not self.allowed():
                    return
                pending.replace(path)
                self.data["frames"].append(
                    {
                        "at": round(time.monotonic() - self.started, 3),
                        "file": path.name,
                        **details,
                    }
                )
                self._save()
        finally:
            pending.unlink(missing_ok=True)

    def _run(self, port):
        previous = None
        next_frame = 0
        reason = "Показ завершён пользователем"
        try:
            while not self.stop.is_set():
                now = time.monotonic()
                if not self.allowed():
                    reason = "Запись остановлена: помощник выключен"
                    break
                if now - self.started >= MAX_SECONDS:
                    reason = "Достигнут предел записи: 20 минут"
                    self.next_segment = self.automatic
                    break
                if len(self.data["frames"]) >= MAX_FRAMES:
                    reason = "Достигнут предел записи: 240 кадров"
                    self.next_segment = self.automatic
                    break
                try:
                    keys = port.sample()
                except (ValueError, OSError):
                    with self.lock:
                        if previous is not None:
                            self._event("focus_lost")
                        previous = None
                        self.message = "Пауза записи · переключитесь в Warframe"
                    self.stop.wait(0.5)
                    continue
                with self.lock:
                    if keys != previous:
                        self._event("controls", keys=keys)
                        previous = keys
                    self.message = "Записываю показ · управление у вас"
                if now >= next_frame:
                    next_frame = now + FRAME_INTERVAL
                    try:
                        self._frame(port)
                    except ValueError as exc:
                        # A changed foreground is recoverable, storage errors are not.
                        with self.lock:
                            self.message = str(exc)
                self.stop.wait(0.1)
        except Exception as exc:
            reason = "Запись остановлена: " + str(exc)[:180]
        finally:
            with self.lock:
                self.active = False
                self.message = reason
                self.data["state"] = "finished"
                self.data["reason"] = reason
                try:
                    self._save()
                    (self.folder / "report.txt").write_text(self.report(), encoding="utf-8")
                except OSError as exc:
                    self.message = "Не удалось сохранить итог: " + str(exc)[:150]

    def mark(self, label):
        label = str(label).casefold().strip().replace("ё", "е")
        if label not in LABELS:
            raise ValueError("Метки: " + ", ".join(LABELS))
        with self.lock:
            if not self.active:
                raise ValueError("Сначала скажите «начни показ аполлон»")
            self._event("label", label=label, source="user")
            self._save()
        return "Метка сохранена: " + LABELS[label] + ". Источник — ваши слова."

    def observe(self, vision):
        with self.lock:
            if not self.active:
                return
            observed = vision.get("observed", 0)
            if observed <= self._seen_observation or not 0 <= time.time() - observed <= 40:
                return
            self._seen_observation = observed
            code = vision.get("screen", "unknown")
            if code == "unknown":
                self._candidate = self._confirmed = ""
                return
            if code == self._candidate and code != self._confirmed:
                self._confirmed = code
                self._event(
                    "screen",
                    screen=code,
                    label=vision.get("label", "")[:80],
                    source="ocr-two-frames",
                )
            self._candidate = code

    def snapshot(self):
        with self.lock:
            events = self.data["events"] if self.data else []
            labels = [e for e in events if e["kind"] == "label"]
            elapsed = (
                time.monotonic() - self.started
                if self.active
                else (self.data["duration"] if self.data else 0)
            )
            return {
                "active": self.active,
                "profile": copy.deepcopy(PROFILE),
                "message": self.message,
                "seconds": int(elapsed),
                "frames": len(self.data["frames"]) if self.data else 0,
                "labels": len(labels),
                "last_label": LABELS[labels[-1]["label"]] if labels else "",
                "folder": str(self.folder) if self.folder else "",
                "reward_marks": sum(e["label"] == "награда" for e in labels),
                "autonomous": False,
                "automatic": self.automatic,
            }

    def panel(self):
        state = self.snapshot()
        return {
            "title": "Аполлон · запись показа",
            "lines": [
                "Валькирия Прайм · цель: Акси",
                state["message"],
                f"Время: {state['seconds'] // 60}:{state['seconds'] % 60:02d} · кадров: {state['frames']}",
                "Метка: " + (state["last_label"] or "ещё нет"),
                f"Отметок награды: {state['reward_marks']} · добыча не проверена",
                "«Закончи показ варфрейм» — сохранить"
                if state["active"]
                else "Отчёт доступен в меню «Обучение»",
            ],
            "until": time.time() + 3,
        }

    def report(self):
        state = self.snapshot()
        return "\n".join(
            [
                "Профиль: Аполлон · Луа · Сбой. Цель: реликвии Акси. Валькирия Прайм.",
                PROFILE["source"],
                state["message"],
                f"Время: {state['seconds'] // 60} мин {state['seconds'] % 60} сек; кадров: {state['frames']}; меток: {state['labels']}.",
                f"Отметок получения награды со слов игрока: {state['reward_marks']}. Количество реликвий не подтверждено.",
                "Это запись показов. Автономный запуск миссии, навигация и бой не обучены.",
                "Файлы: " + (state["folder"] or "запись ещё не начата"),
            ]
        )

    def learning_report(self):
        from game_vision import SCREENS

        with self.lock:
            history = dict(self.history)
            if self.data:
                history[self.data["id"]] = self.summary
            labels, controls, screens = Counter(), Counter(), Counter()
            for row in history.values():
                labels.update(row["labels"])
                controls.update(row["controls"])
                screens.update(row["screens"])
            frames = sum(row["frames"] for row in history.values())
            minutes = sum(row["seconds"] for row in history.values()) // 60
            examples = "; ".join(f"{LABELS[k]}: {labels[k]}" for k in LABELS if labels[k])
            actions = "; ".join(
                f"{name}: {controls[key]}"
                for key, name in (
                    ("space", "прыжок"),
                    ("melee", "клавиша ближнего боя"),
                    ("ability4", "клавиша 4"),
                    ("interact", "взаимодействие"),
                )
                if controls[key]
            )
            recognized = "; ".join(
                f"{label}: {screens[code]}" for code, label, _ in SCREENS if screens[code]
            )
            not_seen = ", ".join(label for code, label, _ in SCREENS if not screens[code])
            return "\n".join(
                [
                    "Аполлон · Луа · Акси · Валькирия Прайм",
                    f"Последние {len(history)} показов (до 100): {minutes} мин · кадров: {frames}",
                    "Уже работает: распознавание нескольких экранов, запись показов и голосовых меток.",
                    "Примеры со слов игрока: " + (examples or "ещё не отмечены"),
                    "Замеченные нажатия: "
                    + (actions or "ещё нет данных")
                    + ". Раскладка стандартная; это не подтверждение срабатывания способности.",
                    f"Наблюдений знакомых экранов по двум OCR-кадрам: {sum(screens.values())}.",
                    "Уже различала: " + (recognized or "пока нет подтверждённых экранов"),
                    "Ещё нет примеров: "
                    + (not_seen or "примеры есть для всех текущих типов экранов"),
                    "Для обучения навигации ещё нужны положение цели и движение камеры. Текущая запись этого не восстанавливает.",
                    "Автономный запуск миссии, поиск демолиста, бой и эвакуация: НЕ ОБУЧЕНЫ / НЕ ПРОВЕРЕНЫ.",
                    "Статистика нажатий не является обученной моделью управления. Проверенных автономных сценариев: 0.",
                    self.history_error,
                ]
            ).strip()

    def finish(self):
        self.stop.set()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        return self.report()

    def close(self):
        self.finish()
