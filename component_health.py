"""Evidence-based component checks, sharing the existing recovery owners."""

from collections import deque
import copy
import threading
import time


class ComponentHealth:
    def __init__(self, runtime, root):
        self.runtime = runtime
        self.path = root / "component_health.json"
        self.lock = threading.RLock()
        self.check_lock = threading.Lock()
        self.rows = {}
        self.history = deque(maxlen=60)
        self.attempts = {}
        self.last_tick = 0
        self.worker = None
        try:
            import json

            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.history.extend(r for r in data.get("history", [])[-60:] if isinstance(r, dict))
        except (OSError, ValueError, AttributeError, TypeError):
            pass

    def snapshot(self):
        with self.lock:
            return {
                "components": copy.deepcopy(self.rows),
                "history": list(self.history)[-8:],
                "checking": self.check_lock.locked(),
            }

    def report(self):
        rows = self.snapshot()["components"]
        return (
            "\n".join(r["text"] for r in rows.values())
            or "Проверка подключений ещё не запускалась."
        )

    def _record(self, key, state, text):
        from integrations import redact
        from storage import atomic_write_json

        text = redact(text, self.runtime.settings())[:600]
        row = {"component": key, "state": state, "text": text, "time": time.time()}
        with self.lock:
            previous = self.rows.get(key)
            self.rows[key] = row
            if not previous or (previous["state"], previous["text"]) != (state, text):
                self.history.append(row)
                atomic_write_json(self.path, {"history": list(self.history)})

    def _allow_repair(self, key):
        if not self.runtime.settings().get("app_settings", {}).get("self_recovery", True):
            return False
        now = time.monotonic()
        values = [v for v in self.attempts.get(key, []) if now - v < 600]
        self.attempts[key] = values
        if len(values) >= 3 or (values and now - values[-1] < 60):
            return False
        values.append(now)
        return True

    def request(self, components=("voice", "bots", "music"), repair=True):
        with self.lock:
            if self.worker and self.worker.is_alive():
                return "Проверка уже выполняется. Результат появится здесь."

            def run():
                try:
                    self.check(components, repair=repair)
                except Exception as exc:
                    self.runtime.events.put(("error", "Диагностика подключений: " + str(exc)[:200]))

            self.worker = threading.Thread(target=run, daemon=True, name="krest-health")
            self.worker.start()
        return "Проверяю подключения. Повторно отправлять команды приложениям не буду."

    def tick(self):
        now = time.monotonic()
        if now - self.last_tick < 30:
            return
        self.last_tick = now
        # Memory-only read. No device enumeration, COM, process scan or model load.
        import voice

        thread = self.runtime._voice_thread
        if thread and not self.runtime._voice_stop.is_set():
            data = voice.telemetry()
            if not thread.is_alive() or not data.get("connected") or "voice" in self.rows:
                self.request(("voice",))

    def check(self, components=("voice", "bots", "music"), *, repair=True, guard=None):
        guard = guard or (lambda: None)
        if not self.check_lock.acquire(blocking=False):
            return "Проверка уже выполняется."
        try:
            for component in components:
                guard()
                if self.runtime.stop_event.is_set():
                    return "Крест завершает работу."
                # Paused raised by guard must propagate to the AFK queue.
                try:
                    getattr(self, "_" + component)(repair)
                except Exception as exc:
                    self._record(component, "attention", component + ": " + str(exc))
            return self.report()
        finally:
            self.check_lock.release()

    def _voice(self, repair):
        import voice

        rt = self.runtime
        if rt._voice_stop.is_set() or not rt._voice_thread:
            self._record("voice", "stopped", "Микрофон: голос выключен или ещё не запускался.")
            return
        data = voice.telemetry()
        if not rt._voice_thread.is_alive():
            if repair and self._allow_repair("voice"):
                rt.start_voice()
                self._record(
                    "voice", "recovering", "Голос: перезапущен поток; ожидаю свежий звук микрофона."
                )
            else:
                self._record(
                    "voice",
                    "attention",
                    "Голос: поток остановился. Автовосстановление выключено или исчерпаны 3 попытки за 10 минут; проверьте модели и устройство.",
                )
        elif (
            data.get("connected")
            and data.get("audio_age_seconds") is not None
            and data["audio_age_seconds"] < 5
        ):
            hint = data.get("mic_hint", "")
            self._record(
                "voice",
                "attention" if hint else "healthy",
                "Микрофон: "
                + (
                    hint
                    or "свежие аудиоблоки поступают; " + data.get("device_name", "вход подключён")
                ),
            )
        else:
            self._record(
                "voice",
                "recovering",
                "Микрофон: нет свежего звука. "
                + str(
                    data.get("model_error")
                    or rt.voice_status
                    or "Драйвер переподключается с задержкой между попытками."
                ),
            )

    def _bots(self, repair):
        manager = getattr(self.runtime, "integrations", None)
        if manager is None:
            self._record("bots", "stopped", "Боты: менеджер подключений не запущен.")
            return
        if repair and self.runtime.settings().get("app_settings", {}).get("self_recovery", True):
            manager.refresh()  # Sole owner of retry budgets and child processes.
        for filename, title in list(manager.MODULES.values()):
            data = manager.details(filename)
            bridge = data.get("bridge", {})
            state = "stopped"
            reason = "выключен"
            if bridge.get("auth_required"):
                state, reason = (
                    "attention",
                    "нужен вход в аккаунт; автоматический перезапуск не поможет",
                )
            elif data.get("error"):
                state, reason = "attention", data["error"]
            elif data["running"]:
                fresh = time.time() - bridge.get("last_health", 0) <= bridge.get(
                    "health_timeout", 120
                )
                if bridge.get("monitored") and bridge.get("ready") and fresh:
                    state, reason = "healthy", "соединение подтверждено свежей проверкой"
                elif bridge.get("monitored"):
                    state, reason = (
                        "recovering",
                        bridge.get("diagnostic") or "нет подтверждения соединения",
                    )
                else:
                    state, reason = (
                        "unknown",
                        "процесс работает, но скрипт не сообщает состояние соединения",
                    )
            if data.get("restarts", 0) >= 3 and state == "recovering":
                state, reason = (
                    "attention",
                    "исчерпаны попытки восстановления; проверьте сеть и журнал",
                )
            self._record("bot:" + filename, state, title + ": " + reason)

    def _music(self, repair):
        import media_control

        preferred = self.runtime.settings().get("media", {}).get("preferred_app", "")
        try:
            rows = media_control.query(all_sessions=True)
            selected = (
                [r for r in rows if r.get("app_id") == preferred]
                if preferred
                else [r for r in rows if r.get("selected")]
            )
            if not rows:
                self._record(
                    "music", "stopped", "Музыка: активных медиасессий нет; запустите трек в плеере."
                )
            elif len(selected) != 1:
                self._record(
                    "music",
                    "attention",
                    "Музыка: источник недоступен или неоднозначен; выберите плеер во вкладке «Музыка».",
                )
            else:
                row = selected[0]
                with self.runtime.verifier.lock:
                    recent = next(
                        (
                            r
                            for r in reversed(self.runtime.verifier.rows)
                            if r["skill"].startswith("skills.media.")
                        ),
                        None,
                    )
                if recent and recent["status"] in {"failed", "unconfirmed"}:
                    repaired = False
                    if repair and self._allow_repair("music"):
                        from desktop_transport import reset_idle

                        repaired = reset_idle("music")
                    self._record(
                        "music",
                        "attention",
                        "Музыка: "
                        + row["app_name"]
                        + " доступна, но последняя команда не подтверждена. "
                        + recent["text"]
                        + (
                            " Помощник подготовлен к следующему обращению; действие не повторялось."
                            if repaired
                            else ""
                        ),
                    )
                else:
                    self._record(
                        "music",
                        "healthy",
                        "Музыка: "
                        + row["app_name"]
                        + " отвечает на запрос состояния. Результаты действий — в журнале подтверждений.",
                    )
        except Exception:
            if repair and self._allow_repair("music"):
                from desktop_transport import reset_idle

                reset_idle("music")
                self._record(
                    "music",
                    "recovering",
                    "Музыка: помощник сброшен для следующего обращения; переключение и лайк повторно не отправлялись.",
                )
            raise

    def call(self, action):
        if action == "snapshot":
            return self.snapshot()
        if action == "report":
            return self.report()
        if action in {"check", "repair"}:
            return self.request(repair=action == "repair")
        raise ValueError("Неизвестное действие диагностики")
