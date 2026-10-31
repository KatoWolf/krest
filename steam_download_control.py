"""Pause only active Steam downloads; never use Resume All or steal focus."""

import hashlib
import os
from pathlib import Path
import re
import threading
import time
import psutil
from storage import atomic_write_json, read_json


def manifest(path):
    from autonomy_files import clean_path

    path = clean_path(path)
    if path.stat().st_size > 512 * 1024:
        raise ValueError("Слишком большой манифест Steam")
    raw = path.read_bytes()
    data = dict(re.findall(r'"(\w+)"\s+"([^"\r\n]*)"', raw.decode("utf-8", errors="replace")))
    if not data.get("appid", "").isdigit() or not data.get("name"):
        raise ValueError("Steam ещё не записал сведения о загрузке")
    flags = int(data.get("StateFlags", "0"))
    return {
        "appid": data["appid"],
        "name": data["name"],
        "path": str(path),
        "flags": flags,
        "paused": bool(flags & 512),
        "active": bool(flags & 1048576 and not flags & 512),
        "signature": hashlib.sha256(raw).hexdigest(),
    }


def steam_identity():
    found = []
    for p in psutil.process_iter(["name", "create_time"]):
        try:
            if (p.info.get("name") or "").casefold() == "steam.exe":
                found.append({"pid": p.pid, "created": p.info["create_time"]})
        except psutil.Error:
            pass
    if len(found) != 1:
        raise ValueError("Не найден единственный запущенный Steam")
    return found[0]


def active_downloads(settings):
    from steam_service import local_games

    result = []
    for row in local_games(settings.get("steam", {}).get("install_path", ""))[:500]:
        try:
            value = manifest(
                Path(row["library"]) / "steamapps" / ("appmanifest_" + str(row["appid"]) + ".acf")
            )
            if value["active"]:
                result.append(value)
        except (OSError, ValueError):
            continue
    return result[:3]


def invoke(action, row, identity):
    from desktop_transport import request

    result = request(
        {
            "operation": "steam_download",
            "action": action,
            "data": {"name": row["name"], **identity},
        },
        timeout=6,
    )
    if result.get("error"):
        raise ValueError(result["error"])
    if not result.get("result", {}).get("invoked"):
        raise ValueError("Steam не подтвердил обращение к кнопке")


def native_action(action, data):
    """Runs in the existing isolated UIA helper. Exact per-game button only."""
    if (
        action not in ("pause", "resume")
        or not isinstance(data.get("name"), str)
        or not 1 <= len(data["name"]) <= 250
    ):
        raise ValueError("Неизвестное действие Steam")
    root = psutil.Process(int(data["pid"]))
    if (
        root.name().casefold() != "steam.exe"
        or abs(root.create_time() - float(data["created"])) > 0.1
    ):
        raise ValueError("Процесс Steam изменился")
    pids = {root.pid, *[p.pid for p in root.children(recursive=True)]}
    from pywinauto import Desktop

    labels = {
        "pause": {
            "pause",
            "pause download",
            "pause downloading",
            "приостановить",
            "приостановить загрузку",
            "пауза",
        },
        "resume": {
            "resume",
            "resume download",
            "resume downloading",
            "возобновить",
            "возобновить загрузку",
            "продолжить загрузку",
        },
    }[action]
    candidates = []
    for window in Desktop(backend="uia").windows(visible_only=False):
        if window.element_info.process_id not in pids:
            continue
        buttons = window.descendants(control_type="Button")
        if len(buttons) > 400:
            continue
        for button in buttons:
            if (
                button.element_info.name or ""
            ).strip().casefold() not in labels or not button.is_enabled():
                continue
            parent = button
            for _ in range(4):
                parent = parent.parent()
                if parent is None or parent == window:
                    break
                texts = parent.descendants(control_type="Text")
                if len(texts) > 60:
                    break
                if not any(t.element_info.name == data["name"] for t in texts):
                    continue
                controls = parent.descendants(control_type="Button")
                matching = [
                    b for b in controls if (b.element_info.name or "").strip().casefold() in labels
                ]
                if len(matching) == 1:
                    candidates.append(button)
                break
    if len(candidates) != 1:
        raise ValueError(
            "Не найдена единственная кнопка загрузки "
            + data["name"]
            + ". Откройте раздел «Загрузки» в Steam и оставьте его доступным. Окно автоматически не переключаю."
        )
    # InvokePattern is allowed to work offscreen; never substitute click_input,
    # a hotkey, a coordinate or a global Resume All button.
    try:
        candidates[0].iface_invoke.Invoke()
    except Exception as exc:
        raise ValueError("Steam не предоставил прямое нажатие этой кнопки") from exc
    return {"invoked": True}


class SteamDownloads:
    def __init__(
        self,
        runtime,
        root,
        reader=manifest,
        scan=active_downloads,
        identity=steam_identity,
        action=invoke,
    ):
        self.runtime = runtime
        self.path = Path(root) / "steam_download_lease.json"
        self.reader, self.scan, self.identity, self.action = reader, scan, identity, action
        self.lock = threading.RLock()
        self.worker = None
        self.closed = False
        self.wanted = False
        self.next_check = 0
        self.attempts = []
        self.ignored = set()
        self.attention = []
        self.message = "Автопауза Steam выключена"
        self.error = ""
        try:
            self.records = read_json(self.path)
            if not isinstance(self.records, dict):
                raise ValueError("Invalid journal")
            for ident, record in self.records.items():
                if (
                    not str(ident).isdigit()
                    or not isinstance(record, dict)
                    or any(
                        not isinstance(record.get(k), str)
                        for k in ("name", "path", "phase", "signature")
                    )
                    or not isinstance(record.get("identity"), dict)
                ):
                    raise ValueError("Invalid record")
        except FileNotFoundError:
            self.records = {}
        except (OSError, ValueError, TypeError):
            self.records = {}
            self.error = "Журнал паузы Steam повреждён. Проверьте загрузки вручную; автоматические нажатия заблокированы."
        self.recovered = bool(self.records)

    def save(self):
        atomic_write_json(self.path, self.records)

    def snapshot(self):
        return {
            "message": self.error or self.message,
            "busy": bool(self.worker and self.worker.is_alive()),
            "paused": [r.get("name", "Игра") for r in list(self.records.values())],
            "retry_seconds": max(0, round(self.next_check - time.monotonic()))
            if self.attempts
            else 0,
        }

    def tick(self, active):
        cfg = self.runtime.settings()
        wanted = bool(
            active
            and cfg.get("download_guard", {}).get("steam_enabled")
            and not cfg.get("safe_mode")
            and not self.closed
        )
        if wanted != self.wanted:
            self.wanted = wanted
            self.next_check = 0
            if not wanted:
                self.ignored.clear()
        if (
            self.error
            or self.worker
            and self.worker.is_alive()
            or time.monotonic() < self.next_check
            or not wanted
            and not self.records
        ):
            return
        self.next_check = time.monotonic() + 30
        self.worker = threading.Thread(target=self._work, daemon=True, name="krest-steam-downloads")
        self.worker.start()

    def _confirmed(self, row, paused):
        deadline = time.monotonic() + 2
        while True:
            data = self.reader(row["path"])
            if data["paused"] == paused:
                return data
            if time.monotonic() >= deadline:
                raise OSError(
                    "Steam не подтвердил смену состояния загрузки; нажатие не повторяется"
                )
            time.sleep(0.1)

    def reconcile(self, wanted):
        identity = self.identity()
        for ident, record in list(self.records.items()):
            row = self.reader(record["path"])
            if row["appid"] != ident:
                raise ValueError("Идентификатор загрузки Steam изменился")
            if record["phase"] == "resume_requested":
                self.message = (
                    "Возобновление "
                    + record["name"]
                    + (
                        " подтверждено"
                        if not row["paused"]
                        else " не подтверждено; повторного нажатия не будет"
                    )
                )
                self.attention = [self.message]
                del self.records[ident]
                self.save()
                self.ignored.add(ident)
                continue
            if record["identity"] != identity:
                self.message = "Steam перезапускался; сохранённые загрузки проверьте вручную"
                self.attention = [self.message]
                del self.records[ident]
                self.save()
                self.ignored.add(ident)
                continue
            if record["phase"] not in ("paused", "resume_requested"):
                # A timeout after Invoke is uncertain. No blind repeat on restart.
                self.message = (
                    "Результат паузы " + record["name"] + " не подтверждён; проверьте в Steam"
                )
                self.attention = [self.message]
                del self.records[ident]
                self.save()
                self.ignored.add(ident)
                continue
            if row["signature"] != record["signature"]:
                self.message = (
                    "Состояние " + record["name"] + " изменилось; ручное управление сохранено"
                )
                self.attention = [self.message]
                del self.records[ident]
                self.save()
                self.ignored.add(ident)
                continue
            if not wanted:
                if row["paused"]:
                    record["phase"] = "resume_requested"
                    self.save()
                    self.action("resume", row, identity)
                    self._confirmed(row, False)
                del self.records[ident]
                self.save()
                self.message = "Возобновлены только подтверждённые загрузки, остановленные Крест"
        if wanted:
            for row in self.scan(self.runtime.settings()):
                ident = row["appid"]
                if ident in self.records or ident in self.ignored or not row["active"]:
                    continue
                record = {**row, "identity": identity, "phase": "pause_requested"}
                self.records[ident] = record
                self.save()
                self.action("pause", row, identity)
                confirmed = self._confirmed(row, True)
                record.update(signature=confirmed["signature"], phase="paused")
                self.save()
                self.message = "Steam: приостановлена загрузка " + row["name"]
            if not self.records:
                self.message = (
                    self.attention[-1]
                    if self.attention
                    else "Steam: активных доступных загрузок для паузы нет"
                )

    def _work(self):
        try:
            self.reconcile(self.wanted and not self.recovered and not self.closed)
            if self.closed and self.records:
                self.reconcile(False)
            self.recovered = False
            self.attempts.clear()
        except Exception as exc:
            now = time.monotonic()
            self.attempts = [t for t in self.attempts if now - t < 600] + [now]
            self.next_check = now + 30 if len(self.attempts) < 3 else self.attempts[0] + 600
            self.message = str(exc)[:450]

    def close(self):
        self.closed = True
        self.tick(False)
        if self.worker:
            self.worker.join(timeout=2)
