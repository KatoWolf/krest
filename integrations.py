"""Manage only child services owned by this panel, with bounded redacted logs."""

from pathlib import Path
import os
import re
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
import logging
import json

BASE_DIR = Path(__file__).resolve().parent


def redact(text, settings):
    code = settings.get("license", {}).get("code", "")
    if code:
        text = text.replace(code, "[код скрыт]")
    for service in settings.get("services", {}).values():
        if isinstance(service, dict) and service.get("token"):
            text = text.replace(str(service["token"]), "[скрыто]")
    values = settings.get("api_keys", {})
    for key in (
        "krest_bot_token",
        "discord_token",
        "discord_user_token",
        "telegram_api_hash",
        "steam_api_key",
    ):
        value = os.getenv(
            {
                "krest_bot_token": "KREST_BOT_TOKEN",
                "discord_token": "DISCORD_TOKEN",
            }.get(key, ""),
            "",
        ) or values.get(key)
        if value:
            text = text.replace(str(value), "[скрыто]")
    return re.sub(r"bot\d{5,}:[A-Za-z0-9_-]+", "bot[скрыто]", text)


class IntegrationManager:
    BUILTIN_COMMANDS = {
        "ds_session.py": {"refresh", "channels", "send_message"},
        "tg_bridge.py": {"reply"},
        "discord_account.py": {"reply"},
    }
    MODULES = {
        "tg_user": ("tg_bridge.py", "Telegram User"),
        "tgt": ("krest_bot.py", "Telegram Terminal"),
        "discord": ("ds_session.py", "Discord Bot"),
        "discord_account": ("discord_account.py", "Discord — личная сессия"),
        "steam": ("steam_bot.py", "Steam"),
        "presence": ("discord_presence.py", "Discord — мой статус"),
    }

    def __init__(self, app, base_dir=BASE_DIR):
        self.app = app
        self.base_dir = Path(base_dir)
        from app_paths import compiled, data_dir

        self.log_dir = (data_dir() if compiled() else self.base_dir) / "logs"
        self.processes, self.errors, self.started_at, self.log_handles = {}, {}, {}, {}
        self._readers = {}
        self._lock = threading.RLock()
        self._health_lock = threading.Lock()
        self._closed = False
        self.bridge = {}
        self.manifests = {}
        self.restart_history = {}
        self.restart_due = {}
        self._generation = {}
        self.resources = {}
        self._last_resources = 0
        self._recovering = set()
        self.MODULES = dict(type(self).MODULES)
        self.reload_registry()

    def reload_registry(self):
        updated = dict(type(self).MODULES)
        builtins = {filename for filename, title in updated.values()}
        for ident, cfg in self.app.config_mgr.snapshot().get("services", {}).items():
            if ident not in builtins:
                updated[ident] = (ident, cfg.get("title", ident))
        valid = {filename for filename, title in updated.values()}
        for filename in list(self.processes):
            if filename not in valid:
                self.stop(filename)
        self.MODULES = updated

    def path(self, filename, settings=None):
        settings = self.app.config_mgr.snapshot() if settings is None else settings
        custom = settings.get("services", {}).get(filename, {}).get("path")
        return str(Path(custom).expanduser().resolve()) if custom else str(self.base_dir / filename)

    def is_builtin_release(self, filename, settings=None):
        from app_paths import compiled
        from service_runner import BUILTIN_SERVICES

        settings = self.app.config_mgr.snapshot() if settings is None else settings
        custom = settings.get("services", {}).get(filename, {}).get("path")
        return compiled() and filename in BUILTIN_SERVICES and not custom

    def exists(self, filename, settings=None):
        settings = self.app.config_mgr.snapshot() if settings is None else settings
        return (
            self.is_builtin_release(filename, settings)
            or Path(self.path(filename, settings)).is_file()
        )

    def _module_status(self, filename, settings=None):
        if not self.exists(filename, settings):
            return "Не установлен"
        proc = self.processes.get(filename)
        if proc and proc.poll() is None:
            if self.errors.get(filename):
                return "Требует внимания"
            return "Подключён" if self.bridge.get(filename, {}).get("ready") else "Подключение…"
        return "Ошибка запуска" if filename in self.errors else "Не запущен"

    def statuses(self, settings=None):
        """Read all services from one settings snapshot; never run recovery here."""
        settings = self.app.config_mgr.snapshot() if settings is None else settings
        return {
            key: self._module_status(filename, settings)
            for key, (filename, _) in list(self.MODULES.items())
        }

    def details(self, filename):
        """Read-only UI snapshot; never wait for a process being stopped."""
        proc = self.processes.get(filename)
        running = proc is not None and proc.poll() is None
        return {
            "running": running,
            "pid": proc.pid if running else None,
            "uptime": max(0, int(time.time() - self.started_at.get(filename, time.time())))
            if running
            else 0,
            "error": self.errors.get(filename, ""),
            "bridge": self.bridge.get(filename, {}),
            "last_action": self.bridge.get(filename, {}).get("last_action", {}),
            "health": "error"
            if self.errors.get(filename)
            else "ready"
            if self.bridge.get(filename, {}).get("ready") and running
            else "starting"
            if running
            else "stopped",
            "resources": self.resources.get(filename, {}),
            "manifest": self.manifests.get(filename, {}),
            "restarts": len(self.restart_history.get(filename, [])),
            "diagnostic": self.bridge.get(filename, {}).get(
                "diagnostic", "Ожидаю проверку соединения"
            ),
        }

    def send(self, filename, command, **payload):
        from licensing import require_premium

        require_premium(self.app.config_mgr.snapshot())
        with self._lock:
            proc = self.processes.get(filename)
            capabilities = self.bridge.get(filename, {}).get("capabilities", [])
            if proc is None or proc.poll() is not None or command not in capabilities:
                raise RuntimeError("Скрипт не запущен или не объявил поддержку команды " + command)
            if not self.bridge.get(filename, {}).get("ready"):
                raise RuntimeError("Сервис ещё не подтвердил подключение")
            encoded = json.dumps({**payload, "command": command}, ensure_ascii=False)
            if len(encoded) > 8192:
                raise ValueError("Команда моста слишком длинная")
            self.bridge[filename]["last_action"] = {
                "command": command,
                "id": str(payload.get("request_id", "")),
                "status": "pending",
                "time": time.time(),
            }
            try:
                proc.stdin.write(encoded + "\n")
                proc.stdin.flush()
            except (OSError, ValueError):
                self.bridge[filename]["last_action"]["status"] = "failed"
                raise

    def autostart(self):
        for filename, cfg in self.app.config_mgr.snapshot().get("services", {}).items():
            if cfg.get("autostart"):
                try:
                    self.start(filename)
                except Exception as exc:
                    self.errors[filename] = redact(str(exc), self.app.config_mgr.snapshot())

    def _event(self, filename, line, settings):
        from bridge_sdk import PREFIX

        if not line.startswith(PREFIX) or len(line) > 8192:
            return
        try:
            value = json.loads(line[len(PREFIX) :])
        except ValueError:
            return
        if not isinstance(value, dict):
            return
        kind = value.get("type")
        message = redact(str(value.get("text", ""))[:2000], settings)
        permissions = self.manifests.get(filename, {}).get(
            "permissions", ["notifications", "refresh"]
        )
        self.bridge.setdefault(filename, {})["last_event"] = time.time()
        if kind == "ready":
            self.errors.pop(filename, None)
            caps = value.get("capabilities", [])
            custom = settings.get("services", {}).get(filename, {}).get("path")
            allowed = self.BUILTIN_COMMANDS.get(filename, set()) if not custom else set()
            allowed = allowed | ({"refresh"} if "refresh" in permissions else set())
            if filename == "ds_session.py" and "discord_control" in permissions:
                allowed |= {"refresh", "channels", "send_message"}
            self.bridge[filename] = {
                **self.bridge.get(filename, {}),
                "text": message,
                "ready": True,
                "auth_required": False,
                "last_event": time.time(),
                "capabilities": [c for c in caps if c in allowed] if isinstance(caps, list) else [],
            }
        elif kind == "health":
            connected = value.get("connected") is True
            timeout = value.get("timeout", 120)
            timeout = max(120, min(600, timeout)) if type(timeout) is int else 120
            self.bridge[filename].update(
                last_health=time.time(),
                monitored=True,
                ready=connected,
                diagnostic=message,
                pending=value.get("pending", 0),
                replies=value.get("replies", 0),
                health_timeout=timeout,
            )
            if connected:
                self.errors.pop(filename, None)
        elif kind == "auth_required":
            self.errors[filename] = message
            self.bridge[filename].update(ready=False, auth_required=True, diagnostic=message)
        elif kind == "status":
            self.errors.pop(filename, None)
            self.bridge.setdefault(filename, {})["text"] = message
        elif kind == "disconnected":
            self.bridge[filename].update(ready=False, text=message)
        elif kind == "data" and filename in ("ds_session.py", "discord_account.py", "tg_bridge.py"):
            data = value.get("data")
            if isinstance(data, dict):
                self.bridge[filename].setdefault("data", {}).update(data)
                receipt = data.get("action", {})
                previous = self.bridge[filename].get("last_action", {})
                if (
                    isinstance(receipt, dict)
                    and receipt.get("id")
                    and receipt.get("id") == previous.get("id")
                ):
                    self.bridge[filename]["last_action"] = {
                        **previous,
                        "text": redact(str(receipt.get("text", ""))[:500], settings),
                        "status": "confirmed" if receipt.get("ok") else "failed",
                        "time": time.time(),
                    }
                elif previous.get("command") in ("refresh", "channels") and (
                    "overview" in data or "channels" in data
                ):
                    self.bridge[filename]["last_action"] = {
                        **previous,
                        "status": "confirmed",
                        "time": time.time(),
                    }
        elif kind == "error":
            self.errors[filename] = message
        elif (
            kind == "notification"
            and "notifications" in permissions
            and hasattr(self.app, "runtime")
        ):
            priority = value.get("priority", "normal")
            if priority not in ("low", "normal", "high"):
                priority = "normal"
            self.app.runtime.notifications.put(
                message,
                source=filename,
                priority=priority,
                notice_kind="message" if value.get("notice_kind") == "message" else "",
                target=value.get("target")
                if filename in ("tg_bridge.py", "discord_account.py")
                and not settings.get("services", {}).get(filename, {}).get("path")
                else None,
            )

    def refresh(self):
        if not self._health_lock.acquire(blocking=False):
            return self.statuses()
        try:
            return self._refresh()
        finally:
            self._health_lock.release()

    def _refresh(self):
        # Restart only our live child when its advertised health probe stops.
        # Never reset credentials or restart authentication/rate-limit failures.
        now_wall = time.time()
        settings = self.app.config_mgr.snapshot()
        for filename, proc in list(self.processes.items()):
            bridge = self.bridge.get(filename, {})
            cfg = settings.get("services", {}).get(filename, {})
            if (
                proc.poll() is None
                and bridge.get("monitored")
                and not bridge.get("auth_required")
                and cfg.get("auto_restart", True)
                and now_wall - bridge.get("last_health", now_wall)
                > bridge.get("health_timeout", 120)
                and filename not in self._recovering
            ):
                now = time.monotonic()
                history = [t for t in self.restart_history.get(filename, []) if now - t < 600]
                self.restart_history[filename] = history
                if len(history) < 3:
                    self._recovering.add(filename)
                    history.append(now)
                    bridge["diagnostic"] = "Проверка соединения зависла; перезапускаю сервис"
                    generation = self._generation.get(filename, 0)
                    threading.Thread(
                        target=self._repair_stalled, args=(filename, generation), daemon=True
                    ).start()
                else:
                    bridge["diagnostic"] = "Три восстановления за 10 минут. Проверьте сеть и журнал"
        if self._lock.acquire(blocking=False):
            try:
                for filename, proc in list(self.processes.items()):
                    code = proc.poll()
                    if code is not None:
                        if code:
                            self.errors.setdefault(
                                filename,
                                f"Процесс завершился с кодом {code}; подробности в журнале",
                            )
                        self.processes.pop(filename, None)
                        if proc.stdin:
                            proc.stdin.close()
                        cfg = settings.get("services", {}).get(filename, {})
                        if (
                            code
                            and cfg.get("auto_restart", True)
                            and not self._closed
                            and not self.bridge.get(filename, {}).get("auth_required")
                        ):
                            now = time.monotonic()
                            history = [
                                v for v in self.restart_history.get(filename, []) if now - v < 600
                            ]
                            self.restart_history[filename] = history
                            if len(history) < 3:
                                self.restart_due[filename] = now + 5 * 3 ** len(history)
            finally:
                self._lock.release()
        now = time.monotonic()
        if self._lock.acquire(blocking=False):
            try:
                for filename, deadline in list(self.restart_due.items()):
                    if now < deadline or self._closed:
                        continue
                    self.restart_due.pop(filename, None)
                    generation = self._generation.get(filename, 0)
                    self.restart_history.setdefault(filename, []).append(now)
                    threading.Thread(
                        target=self._recover_service, args=(filename, generation), daemon=True
                    ).start()
            finally:
                self._lock.release()
        if now - self._last_resources >= 10:
            self._last_resources = now
            import psutil

            for filename, proc in list(self.processes.items()):
                try:
                    process = psutil.Process(proc.pid)
                    self.resources[filename] = {
                        "memory_mb": round(process.memory_info().rss / 1024**2, 1),
                        "threads": process.num_threads(),
                    }
                except psutil.Error:
                    self.resources.pop(filename, None)
        return self.statuses(settings)

    def _recover_service(self, filename, generation):
        with self._lock:
            if (
                self._closed
                or self._generation.get(filename, 0) != generation
                or not self.app.config_mgr.snapshot()
                .get("services", {})
                .get(filename, {})
                .get("auto_restart", True)
            ):
                return
            try:
                self.start(filename)
            except Exception as exc:
                self.errors[filename] = redact(str(exc), self.app.config_mgr.snapshot())

    def _repair_stalled(self, filename, generation):
        try:
            with self._lock:
                if self._closed or self._generation.get(filename, 0) != generation:
                    return
                self.restart(filename)
        except Exception as exc:
            self.errors[filename] = redact(str(exc), self.app.config_mgr.snapshot())
        finally:
            self._recovering.discard(filename)

    def _capture(self, filename, proc, settings):
        path = self.log_dir / (filename + ".log")
        handler = RotatingFileHandler(path, maxBytes=512_000, backupCount=2, encoding="utf-8")
        try:
            while True:
                line = proc.stdout.readline(8193)
                if not line:
                    break
                if len(line) > 8192:
                    while line and not line.endswith("\n"):
                        line = proc.stdout.readline(8193)
                    line = "[слишком длинная строка сервиса пропущена]\n"
                if self.processes.get(filename) is proc:
                    self._event(filename, line, settings)
                record = logging.LogRecord(
                    filename,
                    logging.INFO,
                    "",
                    0,
                    redact(redact(line.rstrip(), settings), self.app.config_mgr.snapshot()),
                    (),
                    None,
                )
                handler.emit(record)
        finally:
            proc.stdout.close()
            handler.close()

    def start(self, filename):
        from licensing import require_premium

        require_premium(self.app.config_mgr.snapshot())
        with self._lock:
            if self._closed:
                raise RuntimeError("Крест завершает работу; новые процессы не запускаются")
            if not self.exists(filename):
                raise FileNotFoundError(filename)
            proc = self.processes.get(filename)
            if proc and proc.poll() is None:
                return "Уже запущен"
            from diagnostics import validate_module

            settings = self.app.config_mgr.snapshot()
            custom = settings.get("services", {}).get(filename, {}).get("path")
            if not custom:
                validate_module(filename, settings, self.app.config_mgr.filepath.parent)
            from plugin_manifest import inspect_plugin

            self.manifests[filename] = (
                {"api_version": 1, "permissions": ["notifications", "refresh"], "manifest": False}
                if self.is_builtin_release(filename)
                else inspect_plugin(self.path(filename))
            )
            if Path(self.path(filename)).suffix.lower() != ".py":
                raise ValueError("Выберите Python-скрипт .py")
            self.log_dir.mkdir(parents=True, exist_ok=True)
            env = dict(
                os.environ,
                PYTHONIOENCODING="utf-8",
                KREST_CONFIG_PATH=str(self.app.config_mgr.filepath),
                KREST_DATA_DIR=str(self.app.config_mgr.filepath.parent),
                KREST_SERVICE_ID=filename,
                KREST_SERVICE_TOKEN=settings.get("services", {}).get(filename, {}).get("token", "")
                or (
                    settings.get("api_keys", {}).get("discord_token", "")
                    if filename == "ds_session.py"
                    else ""
                ),
                PYTHONPATH=str(self.base_dir) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            )
            from app_paths import compiled, app_command

            command = (
                app_command("--service", filename)
                if self.is_builtin_release(filename)
                else app_command("--external-script", self.path(filename))
                if compiled()
                else [sys.executable, "-u", self.path(filename)]
            )
            if custom:
                folder = Path(self.path(filename)).parent
                interpreters = [
                    folder / name / executable
                    for name in (".venv", "venv")
                    for executable in ("Scripts/python.exe", "bin/python")
                ]
                interpreter = next((path for path in interpreters if path.is_file()), None)
                if interpreter:
                    command = [str(interpreter), "-u", self.path(filename)]
            proc = subprocess.Popen(
                command,
                cwd=Path(self.path(filename)).parent if custom else self.base_dir,
                env=env,
                stdout=subprocess.PIPE,
                stdin=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            )
            self.restart_due.pop(filename, None)
            self.processes[filename] = proc
            self.started_at[filename] = time.time()
            self.errors.pop(filename, None)
            self.bridge.pop(filename, None)
            reader = threading.Thread(
                target=self._capture, args=(filename, proc, settings), daemon=True
            )
            self._readers[filename] = reader
            reader.start()
            return "Запущен"

    def stop(self, filename):
        with self._lock:
            self._generation[filename] = self._generation.get(filename, 0) + 1
            self.restart_due.pop(filename, None)
            proc = self.processes.pop(filename, None)
            if proc is None:
                return "Не запущен"
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
            reader = self._readers.pop(filename, None)
            if reader:
                reader.join(timeout=1)
            if proc.stdin:
                proc.stdin.close()
            return "Остановлен"

    def restart(self, filename):
        with self._lock:
            self.stop(filename)
            return self.start(filename)

    def enforce_license(self):
        from licensing import status

        if not status(self.app.config_mgr.snapshot())["premium"]:
            for filename in list(self.processes):
                self.stop(filename)

    def stop_all(self):
        with self._lock:
            self._closed = True
            for filename in list(self.processes):
                self.stop(filename)

    def log_tail(self, filename, limit=6000):
        path = self.log_dir / (filename + ".log")
        if not path.exists():
            return "Журнал пока пуст."
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - limit))
            return redact(
                stream.read().decode("utf-8", errors="replace"),
                self.app.config_mgr.snapshot(),
            )
