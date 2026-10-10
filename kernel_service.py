"""Authenticated loopback JSON API for the persistent, per-user Krest kernel.

No pickle, shell evaluation or general object reflection is accepted over IPC.
"""

from collections import deque
from dataclasses import asdict, is_dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
import hmac
import json
import logging
import os
import sys
import queue
import secrets
import signal
import threading
import time

from app_runtime import AssistantRuntime
from configuration import ConfigManager
from integrations import IntegrationManager, redact
from process_lock import ProcessLock
from secret_store import protect_bootstrap
from storage import atomic_write_json

PROTOCOL_VERSION = 1


class Kernel:
    def __init__(self, config_path, safe=False):
        self.safe = safe
        self.config = ConfigManager(config_path)
        self.runtime = AssistantRuntime(self.settings, data_dir=Path(config_path).parent)
        self.integrations = IntegrationManager(
            SimpleNamespace(config_mgr=self.config, runtime=self.runtime)
        )
        self.runtime.integrations = self.integrations
        self.stop = threading.Event()
        self.safe = safe
        self.sequence = 0
        self.events = deque(maxlen=1000)
        self._events_lock = threading.RLock()
        self._ready = False
        self.started = time.time()
        self._activation_lock = threading.RLock()
        from windows_events import WindowsEvents

        self.windows = WindowsEvents(self.system_event)
        from kernel_tray import KernelTray

        self.tray = KernelTray(self)
        self._ui_spawn_lock = threading.Lock()
        self._last_ui_spawn = 0
        from game_overlay import OverlayManager

        self.overlay = OverlayManager(self.config.filepath)

    def open_ui(self, quick=False):
        with self._ui_spawn_lock:
            lock = ProcessLock(self.config.filepath.parent / ".ui.lock")
            if lock.acquire():
                lock.close()
                if time.monotonic() - self._last_ui_spawn > 3:
                    import subprocess
                    import sys

                    self._last_ui_spawn = time.monotonic()
                    args = [
                        sys.executable,
                        str(Path(__file__).with_name("main.py")),
                        "--config",
                        str(self.config.filepath),
                    ]
                    if quick:
                        args.append("--quick")
                    subprocess.Popen(
                        args,
                        cwd=Path(__file__).parent,
                        env=dict(os.environ, KREST_CONFIG_PATH=str(self.config.filepath)),
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                        if os.name == "nt"
                        else 0,
                    )
            else:
                self.event("quick_panel" if quick else "ui_open", None)

    def settings(self):
        cfg = self.config.snapshot()
        if self.safe:
            cfg["safe_mode"] = True
            cfg["event_rules"] = []
            cfg["app_settings"]["downloads_auto_sort"] = False
            cfg["dota_guide"]["auto_mode"] = False
        return cfg

    def system_event(self, kind, payload):
        if kind == "quick_panel":
            self.open_ui(quick=True)
        else:
            self.runtime.automation.emit(kind, payload)

    def event(self, kind, value):
        with self._events_lock:
            self.sequence += 1
            value = asdict(value) if is_dataclass(value) else value
            self.events.append({"seq": self.sequence, "kind": kind, "value": value})

    def activate(self):
        with self._activation_lock:
            return self._activate()

    def _activate(self):
        if self._ready:
            return True
        if not self.config.snapshot()["profile"]["onboarding_completed"]:
            return False
        self._ready = True
        self.runtime.start()
        self.windows.start()
        self.tray.start()
        if not self.safe:
            threading.Thread(target=self.integrations.autostart, daemon=True).start()
        return True

    def snapshot(self, after=0):
        import voice
        from skills import gid

        rt = self.runtime
        with self._events_lock:
            events = [item for item in self.events if item["seq"] > after]
            seq = self.sequence
        return {
            "protocol": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "started": self.started,
            "state": rt.state.snapshot(),
            "reminders": rt.state.reminders(),
            "flags": {
                k: getattr(rt.state, k)
                for k in ("is_muted", "is_sleeping", "dota_guide_active", "load_error")
            },
            "voice": {
                "enabled": voice.is_enabled(),
                "status": rt.voice_status,
                "listening": bool(rt._voice_thread and rt._voice_thread.is_alive()),
                "telemetry": voice.telemetry(),
            },
            "performance": {
                "active": rt.performance.active,
                "metrics": rt.performance.metrics,
                "game": rt.performance.game,
            },
            "scenario": rt.scenarios.status,
            "warframe": rt.warframe.status(),
            "downloads": rt.download_status,
            "command": rt.command_status(),
            "notifications_mode": rt.notification_mode,
            "modules": self.integrations.MODULES,
            "services": {
                ident: self.integrations.details(ident)
                for ident, _ in list(self.integrations.MODULES.values())
            },
            "service_statuses": self.integrations.refresh(),
            "events": events,
            "sequence": seq,
            "gid": gid.service_status(),
            "safe_mode": self.safe,
            "windows_events_error": self.windows.error,
            "tray": {"ready": self.tray.ready, "error": self.tray.error},
        }

    def dispatch(self, method, args):
        rt = self.runtime
        if method == "environment":
            return {"executable": sys.executable}
        if method == "ping":
            return {"protocol": PROTOCOL_VERSION, "pid": os.getpid(), "safe_mode": self.safe}
        if method == "snapshot":
            return self.snapshot(int(args.get("after", 0)))
        if method == "warframe.snapshot":
            result = rt.warframe.snapshot()
            result["status"]["overlay_error"] = self.overlay.error
            return result
        if method == "warframe.request":
            return rt.warframe.request(args["action"], args.get("values", {}))
        if method == "warframe.result":
            return rt.warframe.result(args["id"])
        if method == "overlay":
            import voice

            return {
                "game": rt.warframe.status(),
                "settings": self.settings()["warframe"],
                "voice": voice.telemetry(),
                "listening": bool(rt._voice_thread and rt._voice_thread.is_alive()),
                "command": rt.current_command,
                "reduced": self.settings()["accessibility"]["reduce_motion"],
            }
        if method == "media_query":
            import media_control

            return media_control.query(self.settings()["media"]["preferred_app"], all_sessions=True)
        if method == "activate":
            return self.activate()
        if method == "ui_open":
            self.event("quick_panel" if args.get("quick") else "ui_open", None)
            return True
        if method == "shutdown":
            self.stop.set()
            return True
        if method == "remote_submit":
            from remote_policy import authorize

            allowed, reason = authorize(str(args["original"]), self.settings())
            if not allowed:
                raise ValueError(reason)
            aliases = {"плей": "включи музыку", "🎵 плей / пауза": "пауза"}
            original = str(args["original"]).strip().casefold()
            return rt.submit(
                aliases.get(original, original),
                source="remote",
                request_key=args.get("request_key"),
            )
        if method == "submit":
            if not self.activate():
                raise ValueError("Сначала завершите знакомство с Крест в меню")
            return rt.submit(str(args["text"]), source="text", request_key=args.get("request_key"))
        if method == "clear_pending":
            return rt.clear_pending()
        if method == "catalog":
            return {
                "errors": rt.manager.errors,
                "entries": [
                    {
                        "id": entry.id,
                        "description": entry.description,
                        "category": entry.category,
                        "keywords": entry.keywords,
                        "examples": entry.examples,
                        "patterns": [v.pattern for v in entry.patterns],
                        "availability": rt.manager.availability(entry),
                    }
                    for entry in rt.manager.catalog()
                ],
            }
        if method == "skill_import":
            return rt.skill_import(**args)
        if method == "match":
            result = rt.manager.match(str(args["text"])[:2000])
            return result[0].id if result else None
        if method == "job_result":
            return rt.jobs.by_request(str(args["request_key"]))
        if method == "jobs":
            return rt.jobs.list()
        if method == "job_cancel":
            return rt.cancel_job(str(args["id"]))
        if method == "state_set":
            if args["key"] not in ("is_muted", "is_sleeping") or type(args["value"]) is not bool:
                raise ValueError("Недопустимое изменение состояния")
            setattr(rt.state, args["key"], args["value"])
            return True
        if method.startswith("state."):
            operations = {
                name: getattr(rt.state, name)
                for name in (
                    "add_timer",
                    "add_alarm",
                    "cancel",
                    "snooze",
                    "complete",
                    "acknowledge",
                    "extend_timer",
                    "move_occurrence",
                    "skip_occurrence",
                )
            }
            operation = operations.get(method[6:])
            if not operation:
                raise ValueError("Неизвестное действие с задачей")
            return operation(*args.get("args", []), **args.get("kwargs", {}))
        if method.startswith("voice."):
            import voice

            operations = {
                "start": rt.start_voice,
                "stop": rt.stop_voice,
                "arm": rt.arm_voice,
                "interrupt": voice.stop_speaking,
            }
            if method[6:] == "speak":
                voice.speak(str(args.get("text", ""))[:4000])
                return True
            if method[6:] not in operations:
                raise ValueError("Неизвестная голосовая команда")
            operations[method[6:]]()
            return True
        if method == "scenario.start":
            rt.scenarios.start(args["name"])
            return True
        if method == "scenario.preview":
            return rt.scenarios.preview(args["name"])
        if method == "scenario.retry":
            rt.scenarios.retry()
            return True
        if method == "scenario.skip_wait":
            rt.scenarios.skip_wait()
            return True
        if method == "scenario.cancel":
            rt.scenarios.cancel()
            return True
        if method == "performance":
            rt.performance.set_mode(args["mode"])
            return True
        if method == "notification_mode":
            if args.get("value") not in (None, "all", "important", "silent"):
                raise ValueError("Неизвестный режим уведомлений")
            rt.notification_mode = args.get("value")
            return True
        if method == "notice":
            return rt.publish_notice(
                str(args["text"])[:4000],
                str(args.get("source", "Меню"))[:80],
                args.get("priority", "normal"),
            )
        if method.startswith("service."):
            name = method[8:]
            if name == "autostart":
                if not self.safe:
                    self.integrations.autostart()
                return True
            if name == "reload_registry":
                self.integrations.reload_registry()
                return True
            if name == "enforce_license":
                self.integrations.enforce_license()
                return True
            if name == "stop_all":
                self.integrations.stop_all()
                return True
            filename = str(args["filename"])
            if filename not in {v[0] for v in self.integrations.MODULES.values()}:
                raise ValueError("Сервис не зарегистрирован")
            if name in ("start", "restart") and self.safe:
                raise ValueError("Безопасный режим: подключение сервисов отключено")
            operations = {
                k: getattr(self.integrations, k) for k in ("start", "stop", "restart", "log_tail")
            }
            if name == "send":
                self.integrations.send(filename, str(args["command"]))
                return True
            if name not in operations:
                raise ValueError("Неизвестная операция сервиса")
            return operations[name](filename)
        if method == "dota":
            from skills import gid

            cfg = self.config.snapshot()["dota_guide"]
            if args["action"] == "start":
                return gid.start_service(cfg)
            if args["action"] == "stop":
                gid.stop_service()
                return True
            if args["action"] == "setup":
                gid.GSI_PORT = cfg["gsi_port"]
                return gid.auto_setup_gsi_config()
        raise ValueError("Неизвестная операция ядра")

    def pump(self):
        self.activate()
        last_services = 0
        while not self.stop.wait(0.1):
            while True:
                try:
                    kind, value = self.runtime.events.get_nowait()
                    self.event(kind, value)
                    if kind == "notification":
                        self.tray.notify(value)
                    if kind == "shutdown":
                        self.stop.set()
                except queue.Empty:
                    break
            if time.monotonic() - last_services >= 1:
                self.integrations.refresh()
                last_services = time.monotonic()
            cfg = self.settings()
            self.overlay.tick(
                self._ready
                and not self.safe
                and cfg["warframe"]["overlay_enabled"]
                and self.runtime.warframe.running
            )
        self.overlay.close()
        self.event("shutdown", None)
        # Let attached menus observe the final event before closing local IPC.
        time.sleep(0.75)
        self.tray.stop()
        self.windows.stop()
        self.integrations.stop_all()
        self.runtime.close()


def serve(config_path, safe=False):
    root = Path(config_path).resolve().parent
    lock = ProcessLock(root / ".kernel.lock")
    if not lock.acquire():
        return 0
    kernel = Kernel(config_path, safe)
    from logging.handlers import RotatingFileHandler

    log_root = root / "logs"
    log_root.mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        log_root / "kernel.log", maxBytes=512_000, backupCount=2, encoding="utf-8"
    )

    class RedactedFormatter(logging.Formatter):
        def format(self, record):
            return redact(super().format(record), kernel.config.snapshot())

    handler.setFormatter(RedactedFormatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            self.request.settimeout(5)
            super().setup()

        def do_POST(self):
            if self.path != "/rpc" or not hmac.compare_digest(
                self.headers.get("Authorization", ""), "Bearer " + token
            ):
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 1 <= length <= 64_000:
                    raise ValueError("Недопустимый размер запроса")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict) or not isinstance(data.get("args", {}), dict):
                    raise ValueError("Ожидался JSON-объект")
                result = kernel.dispatch(str(data.get("method", "")), data.get("args", {}))
                payload = {"ok": True, "result": result}
            except Exception as exc:
                payload = {"ok": False, "error": redact(str(exc), kernel.config.snapshot())}
            encoded = json.dumps(payload, ensure_ascii=False).encode()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
            except OSError:
                pass

    class BoundedServer(ThreadingHTTPServer):
        slots = threading.BoundedSemaphore(16)

        def process_request(self, request, address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, address):
            try:
                super().process_request_thread(request, address)
            finally:
                self.slots.release()

    server = BoundedServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    path = root / "kernel_endpoint.json"
    atomic_write_json(
        path,
        {
            "port": server.server_port,
            "pid": os.getpid(),
            "token": protect_bootstrap(token),
            "protocol": PROTOCOL_VERSION,
        },
    )
    if os.name != "nt":
        path.chmod(0o600)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *args: kernel.stop.set())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        kernel.pump()
    finally:
        server.shutdown()
        server.server_close()
        path.unlink(missing_ok=True)
        logging.getLogger().removeHandler(handler)
        handler.close()
        lock.close()
    return 0
