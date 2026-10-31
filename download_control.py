"""Explicit local download-manager adapters and durable compare-before-restore leases."""

import json
import threading
import time
from pathlib import Path
import requests
from storage import atomic_write_json, read_json

DEFAULTS = {
    "steam_enabled": False,
    "enabled": False,
    "client": "qbittorrent",
    "port": 8080,
    "username": "admin",
    "download_kib": 256,
    "upload_kib": 64,
    "gsi": False,
}


def validate_settings(cfg):
    if any(type(cfg.get(k)) is not bool for k in ("enabled", "gsi", "steam_enabled")) or cfg.get(
        "client"
    ) not in (
        "qbittorrent",
        "aria2",
    ):
        raise ValueError("Диспетчер загрузок: неверные параметры подключения")
    for key, low, high in (
        ("port", 1024, 65535),
        ("download_kib", 16, 102400),
        ("upload_kib", 8, 102400),
    ):
        if type(cfg.get(key)) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"{key}: допустимо от {low} до {high}")
    if not isinstance(cfg.get("username"), str) or len(cfg["username"]) > 100:
        raise ValueError("Логин загрузчика слишком длинный")


class LocalDownloader:
    def __init__(self, cfg, keys):
        self.cfg, self.keys = cfg, keys
        self.session = requests.Session()
        self.session.trust_env = False
        self.base = f"http://127.0.0.1:{cfg['port']}"
        self.authenticated = False

    def request(self, method, path, **kw):
        response = self.session.request(
            method, self.base + path, timeout=(1, 3), allow_redirects=False, **kw
        )
        if response.status_code in (401, 403):
            raise ValueError("Загрузчик отклонил вход. Проверьте локальный Web UI и ключ доступа.")
        if not 200 <= response.status_code < 300:
            raise OSError("Загрузчик вернул HTTP " + str(response.status_code))
        if len(response.content) > 65536:
            raise ValueError("Неожиданно большой ответ загрузчика")
        return response

    def qbt_login(self):
        if self.authenticated:
            self.qbt_mode()
            return
        password = self.keys.get("qbittorrent_password", "")
        if password:
            response = self.request(
                "POST",
                "/api/v2/auth/login",
                data={"username": self.cfg["username"], "password": password},
            )
            if response.text.strip() != "Ok.":
                raise ValueError("qBittorrent не подтвердил вход")
        version = self.request("GET", "/api/v2/app/webapiVersion").text.strip()
        if not version.startswith("2."):
            raise ValueError("Ожидался qBittorrent Web API v2")
        self.qbt_mode()
        self.authenticated = True

    def qbt_mode(self):
        mode = self.request("GET", "/api/v2/transfer/speedLimitsMode").text.strip()
        if mode != "0":
            raise ValueError(
                "В qBittorrent включены альтернативные лимиты скорости. Переключите обычный режим перед подключением Крест."
            )

    def rpc(self, method, options=None):
        params = []
        if self.keys.get("aria2_secret"):
            params.append("token:" + self.keys["aria2_secret"])
        if options is not None:
            params.append(options)
        data = self.request(
            "POST",
            "/jsonrpc",
            json={"jsonrpc": "2.0", "id": "krest", "method": "aria2." + method, "params": params},
        ).json()
        if not isinstance(data, dict) or data.get("id") != "krest" or "error" in data:
            raise ValueError("aria2 отклонила запрос. Проверьте RPC и секретный ключ.")
        return data.get("result")

    @staticmethod
    def number(value):
        import re

        match = re.fullmatch(r"(\d{1,12})([KMG]?)", str(value).upper())
        if not match:
            raise ValueError("Не удалось прочитать ограничение скорости")
        return int(match[1]) * {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[match[2]]

    def limits(self):
        if self.cfg["client"] == "qbittorrent":
            self.qbt_login()
            return {
                key: self.number(
                    self.request("GET", "/api/v2/transfer/" + name + "Limit").text.strip()
                )
                for key, name in (("download", "download"), ("upload", "upload"))
            }
        result = self.rpc("getGlobalOption")
        if not isinstance(result, dict):
            raise ValueError("aria2 не сообщила параметры")
        return {
            key: self.number(result["max-overall-" + key + "-limit"])
            for key in ("download", "upload")
        }

    def set_limit(self, key, value):
        if key not in ("download", "upload") or type(value) is not int or value < 0:
            raise ValueError("Некорректный предел скорости")
        if self.cfg["client"] == "qbittorrent":
            self.qbt_login()
            self.request(
                "POST", "/api/v2/transfer/set" + key.title() + "Limit", data={"limit": str(value)}
            )
        else:
            if (
                self.rpc("changeGlobalOption", {"max-overall-" + key + "-limit": str(value)})
                != "OK"
            ):
                raise ValueError("aria2 не подтвердила изменение")

    def close(self):
        self.session.close()


class DownloadControl:
    def __init__(self, runtime, root, factory=LocalDownloader, clock=time.monotonic):
        self.runtime, self.factory, self.clock = runtime, factory, clock
        self.path = Path(root) / "download_lease.json"
        self.lock = threading.RLock()
        self.worker = None
        self.closed = False
        self.attempts = []
        self.next_try = 0
        self.message = "Выберите поддерживаемый загрузчик и проверьте подключение"
        self.load_error = ""
        try:
            self.lease = read_json(self.path)
            if not isinstance(self.lease, dict):
                raise ValueError("ожидался объект")
            if self.lease:
                if not isinstance(self.lease.get("scope"), dict) or not isinstance(
                    self.lease.get("fields"), dict
                ):
                    raise ValueError("неизвестная структура")
                for key, field in self.lease["fields"].items():
                    if (
                        key not in ("download", "upload")
                        or not isinstance(field, dict)
                        or any(
                            type(field.get(k)) is not int or not 0 <= field[k] <= 2**53
                            for k in ("before", "target")
                        )
                    ):
                        raise ValueError("некорректные значения ограничения")
        except FileNotFoundError:
            self.lease = {}
        except (OSError, ValueError, TypeError):
            self.lease = {}
            self.load_error = "Не удалось прочитать журнал ограничений. Скорость автоматически не меняется; проверьте её в загрузчике и восстановите download_lease.json из копии."
            self.message = self.load_error
        self.recovered = bool(self.lease)
        self.wanted = False
        self._signature = None
        self._settled = None

    @staticmethod
    def scope(cfg):
        return {k: cfg[k] for k in ("client", "port", "username")}

    def save(self):
        atomic_write_json(self.path, self.lease)

    def snapshot(self):
        with self.lock:
            return {
                "message": self.message,
                "limited": bool(self.lease),
                "busy": bool(self.worker and self.worker.is_alive()),
                "recovering": self.recovered,
                "retry_seconds": max(0, round(self.next_try - self.clock())),
                "limits": {
                    k: {
                        "before": v["before"],
                        "during": v["target"],
                        "state": v.get("state", "prepared"),
                    }
                    for k, v in self.lease.get("fields", {}).items()
                },
            }

    def tick(self, active):
        if self.load_error:
            return
        cfg = self.runtime.settings().get("download_guard", DEFAULTS)
        wanted = bool(
            active
            and cfg["enabled"]
            and not self.runtime.settings().get("safe_mode")
            and not self.closed
        )
        signature = (wanted, json.dumps(cfg, sort_keys=True))
        with self.lock:
            self.wanted = wanted
            if signature != self._signature:
                self._signature = signature
                self._settled = None
            if self.worker and self.worker.is_alive() or self.clock() < self.next_try:
                return
            if not self.lease and not wanted or self._settled == signature:
                return
            self.worker = threading.Thread(
                target=self._work, daemon=True, name="krest-download-control"
            )
            self.worker.start()

    def _work(self):
        cfg = self.runtime.settings().get("download_guard", DEFAULTS)
        client = self.factory(cfg, self.runtime.settings().get("api_keys", {}))
        try:
            with self.lock:
                signature = self._signature
                wanted = self.wanted and not self.recovered and not self.closed
            self.reconcile(client, cfg, wanted)
            if self.closed and self.lease:
                self.reconcile(client, cfg, False)
            with self.lock:
                self.recovered = False
                self._settled = signature if wanted == self.wanted else None
                self.attempts.clear()
                self.next_try = 0
        except Exception as exc:
            with self.lock:
                now = self.clock()
                self.attempts = [t for t in self.attempts if now - t < 600] + [now]
                self.next_try = (
                    now + (10 if len(self.attempts) == 1 else 30)
                    if len(self.attempts) < 3
                    else self.attempts[0] + 600
                )
                self.message = (
                    (
                        "Подключение недоступно; сохранённые ограничения требуют восстановления. "
                        if self.lease
                        else "Ограничение не включено. "
                    )
                    + type(exc).__name__
                    + ": "
                    + str(exc)[:180]
                )
        finally:
            client.close()

    def reconcile(self, client, cfg, wanted):
        # This routine is also exercised against a fake server in release checks.
        if self.lease and self.lease.get("scope") != self.scope(cfg):
            raise ValueError("Сначала восстановите скорость на прежнем подключении")
        current = client.limits()
        if not self.lease:
            if not wanted:
                self.message = "Ограничения Крест сняты"
                return
            self.lease = {
                "scope": self.scope(cfg),
                "fields": {},
                "baseline": dict(current),
                "created": time.time(),
            }
            for key in ("download", "upload"):
                limit = cfg[key + "_kib"] * 1024
                before = current[key]
                desired = min(before, limit) if before else limit
                if desired != before:
                    self.lease["fields"][key] = {
                        "before": before,
                        "target": desired,
                        "state": "prepared",
                    }
            self.save()  # Intent is durable before the first request.
        if wanted:
            for key, before in self.lease.get("baseline", {}).items():
                if key not in ("download", "upload") or key in self.lease["fields"]:
                    continue
                desired = (
                    min(before, cfg[key + "_kib"] * 1024) if before else cfg[key + "_kib"] * 1024
                )
                if current[key] == before and desired != before:
                    self.lease["fields"][key] = {
                        "before": before,
                        "target": desired,
                        "state": "prepared",
                    }
                    self.save()
        for key, field in self.lease["fields"].items():
            if field.get("state") in ("restored", "manual"):
                continue
            current = client.limits()[key]
            if wanted:
                desired = (
                    min(field["before"], cfg[key + "_kib"] * 1024)
                    if field["before"]
                    else cfg[key + "_kib"] * 1024
                )
                if (
                    field["state"] == "confirmed"
                    and current == field["target"]
                    and desired != field["target"]
                ):
                    field.update(previous_target=field["target"], target=desired, state="prepared")
                    self.save()
                if current == field["target"]:
                    field["state"] = "confirmed"
                    field.pop("previous_target", None)
                elif (
                    current in (field["before"], field.get("previous_target"))
                    and field["state"] == "prepared"
                ):
                    client.set_limit(key, field["target"])
                    if client.limits()[key] != field["target"]:
                        raise OSError("Загрузчик не подтвердил ограничение")
                    field["state"] = "confirmed"
                    field.pop("previous_target", None)
                else:
                    field["state"] = "manual"  # Respect the user's later change.
            else:
                if (
                    current == field["target"]
                    or field["state"] == "prepared"
                    and current == field.get("previous_target")
                ):
                    client.set_limit(key, field["before"])
                    if client.limits()[key] != field["before"]:
                        raise OSError("Загрузчик не подтвердил возврат скорости")
                    field["state"] = "restored"
                else:
                    field["state"] = "restored" if current == field["before"] else "manual"
            self.save()
        if wanted:
            changed = any(v["state"] == "confirmed" for v in self.lease["fields"].values())
            self.message = (
                "Скорость загрузчика ограничена на время матча"
                if changed
                else "Прежние или ручные ограничения сохранены"
            )
        else:
            manual = any(v["state"] == "manual" for v in self.lease["fields"].values())
            self.lease = {}
            self.save()
            self.message = "Прежняя скорость возвращена" + (
                "; ваши ручные изменения сохранены" if manual else ""
            )

    def test(self):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("Дождитесь изменения скорости")
            cfg = self.runtime.settings().get("download_guard", DEFAULTS)
            client = self.factory(cfg, self.runtime.settings().get("api_keys", {}))
            try:
                data = client.limits()
                self.next_try = 0
                self.attempts.clear()
                self._settled = None
                return (
                    "Подключение проверено. Пределы, байт/с: загрузка "
                    + str(data["download"])
                    + ", отдача "
                    + str(data["upload"])
                    + ". 0 — без ограничения."
                )
            finally:
                client.close()

    def close(self):
        self.closed = True
        self.tick(False)
        worker = self.worker
        if worker:
            worker.join(timeout=2)
