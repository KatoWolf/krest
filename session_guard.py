"""Session protection, on-demand evidence and configured local download control."""

import copy
import os
import threading
import time
import psutil
from download_control import DownloadControl
from game_phase import GamePhase


def sample_system(stop):
    started = time.monotonic()
    cpu0 = psutil.cpu_times()
    disk0 = psutil.disk_io_counters()
    processes = []
    for process in psutil.process_iter(["name"]):
        if len(processes) >= 512:
            break
        try:
            times = process.cpu_times()
            processes.append(
                (process, process.info.get("name") or "Процесс", times.user + times.system)
            )
        except psutil.Error:
            pass
    if stop.wait(0.7):
        raise InterruptedError("Диагностика остановлена")
    elapsed = max(0.1, time.monotonic() - started)
    cpu1 = psutil.cpu_times()
    disk1 = psutil.disk_io_counters()
    # guest time is already included in user/nice on Linux.
    total = sum(
        max(0, getattr(cpu1, k) - getattr(cpu0, k))
        for k in cpu1._fields
        if k not in ("guest", "guest_nice")
    )
    idle = max(0, cpu1.idle - cpu0.idle) + max(
        0, getattr(cpu1, "iowait", 0) - getattr(cpu0, "iowait", 0)
    )
    rows = []
    cores = psutil.cpu_count() or 1
    for process, name, before in processes:
        try:
            times = process.cpu_times()
            usage = max(0, times.user + times.system - before) / elapsed / cores * 100
            if usage >= 0.1:
                rows.append({"name": name[:80], "cpu": round(usage, 1), "pid": process.pid})
        except psutil.Error:
            pass
    memory = psutil.virtual_memory()
    return {
        "cpu": round(max(0, (total - idle) / total * 100), 1) if total else 0,
        "free_mb": round(memory.available / 1024**2),
        "disk_mbps": round(
            max(0, (disk1.read_bytes + disk1.write_bytes - disk0.read_bytes - disk0.write_bytes))
            / elapsed
            / 1024**2,
            1,
        )
        if disk0 and disk1
        else None,
        "top": sorted(rows, key=lambda row: row["cpu"], reverse=True)[:5],
        "duration": round(elapsed, 2),
        "at": time.time(),
    }


class SessionGuard:
    def __init__(self, runtime, root, sampler=sample_system):
        self.runtime = runtime
        self.downloads = DownloadControl(runtime, root)
        from steam_download_control import SteamDownloads

        self.steam = SteamDownloads(runtime, root)
        self.phase = GamePhase(runtime, root)
        self.sampler = sampler
        self.lock = threading.RLock()
        self.diagnose_lock = threading.Lock()
        self.report = {}
        self.next_tick = 0

    def tick(self):
        now = time.monotonic()
        if now < self.next_tick:
            return
        self.next_tick = now + 3
        self.downloads.tick(self.phase.snapshot()["active"])
        self.steam.tick(self.phase.snapshot()["active"])

    def snapshot(self):
        with self.lock:
            report = copy.deepcopy(self.report)
        return {
            "budget": self.runtime.governor.snapshot(),
            "match": self.phase.snapshot(),
            "downloads": self.downloads.snapshot(),
            "steam": self.steam.snapshot(),
            "report": report,
            "checking": self.diagnose_lock.locked(),
        }

    def diagnose(self, problem):
        if problem not in ("music", "game", "voice", "system"):
            raise ValueError("Выберите музыку, игру, голос или систему")
        if not self.diagnose_lock.acquire(blocking=False):
            return "Диагностика уже выполняется; результат появится в защите сессии"
        try:
            measured = self.sampler(self.runtime.stop_event)
            findings = []
            if measured["cpu"] >= 85:
                findings.append(f"Высокая загрузка процессора: {measured['cpu']}%.")
            if measured["free_mb"] < 1024:
                findings.append(f"Мало доступной памяти: {measured['free_mb']} МБ.")
            if measured.get("disk_mbps", 0) is not None and measured["disk_mbps"] >= 40:
                findings.append(
                    f"Идёт интенсивный обмен с диском: {measured['disk_mbps']} МБ/с. По объёму обмена нельзя доказать задержки диска."
                )
            if measured["top"]:
                findings.append(
                    "В этом замере больше CPU использовали: "
                    + ", ".join(r["name"] + " " + str(r["cpu"]) + "%" for r in measured["top"][:3])
                    + "."
                )
            repairs = []
            guard = self.runtime.settings().get("resource_guard", {})
            own = self.runtime.performance.metrics
            limit = (
                guard.get("game_cpu_percent", 4)
                if self.runtime.performance.game
                else guard.get("cpu_percent", 8)
            )
            own_high = 0 <= time.monotonic() - own.get("sampled_at", 0) < 30 and (
                own.get("cpu", 0) > limit or own.get("memory_mb", 0) > guard.get("memory_mb", 1600)
            )
            if own_high:
                findings.append(
                    f"Последний замер Крест: {own.get('cpu', 0)}% CPU, {own.get('memory_mb', 0)} МБ."
                )
                if guard.get("automatic_relief", True):
                    repairs.append(self.runtime.governor.relief())
            import voice

            if problem == "voice":
                info = voice.telemetry()
                findings.append(
                    "Микрофон: "
                    + (
                        "аудиоблоки поступают"
                        if info.get("connected") and (info.get("audio_age_seconds") or 0) < 5
                        else "нет подтверждённого свежего звука"
                    )
                    + "."
                )
                if info.get("mic_error"):
                    findings.append(str(info["mic_error"])[:200])
                if guard.get("automatic_relief", True):
                    self.runtime.health.request(("voice",))
                    repairs.append(
                        "Запрошена ограниченная проверка микрофона; результат — в диагностике подключений."
                    )
            if problem == "music":
                from desktop_transport import health_snapshot, reset_idle

                helper = health_snapshot().get("music", {})
                if helper.get("failed_at"):
                    findings.append("У собственного помощника музыки был неподтверждённый запрос.")
                    if (
                        guard.get("automatic_relief", True)
                        and self.runtime.health._allow_repair("music")
                        and reset_idle("music")
                    ):
                        repairs.append(
                            "Помощник музыки подготовлен заново. Команда плееру не повторялась."
                        )
                findings.append(
                    "Этот замер не проверяет задержки аудиодрайвера и качество потока музыки."
                )
            overload = measured["cpu"] >= 85 or measured["free_mb"] < 1024 or own_high
            conclusion = (
                "Есть признаки нехватки ресурсов; они могут объяснять задержки."
                if overload
                else "По текущему замеру причина задержек не установлена."
            )
            text = conclusion + "\n" + "\n".join(findings + repairs)
            with self.lock:
                self.report = {
                    "problem": problem,
                    "measured": measured,
                    "text": text,
                    "repairs": repairs,
                }
            self.runtime.events.put(("session_diagnosis", None))
            return text
        finally:
            self.diagnose_lock.release()

    def call(self, action, **values):
        if action == "snapshot":
            return self.snapshot()
        if action == "diagnose":
            return self.diagnose(values.get("problem", "system"))
        if action == "relief":
            return self.runtime.governor.relief()
        if action == "match":
            result = self.phase.set_manual(bool(values.get("active")))
            self.next_tick = 0
            self.tick()
            return result
        if action == "downloads.test":
            return self.downloads.test()
        if action == "steam.open":
            if os.name != "nt":
                raise ValueError("Открытие Steam доступно в Windows")
            os.startfile("steam://nav/downloads")
            return "Откройте загрузки Steam; затем можно вернуться в игру. Автопауза использует доступные кнопки без переключения окна."
        if action == "settings":
            resource = values.get("resource", {})
            network = values.get("network", {})
            from resource_governor import DEFAULTS as RESOURCE
            from download_control import DEFAULTS as NETWORK

            if (
                not isinstance(resource, dict)
                or not isinstance(network, dict)
                or set(resource) - RESOURCE.keys()
                or set(network) - NETWORK.keys()
            ):
                raise ValueError("Неизвестные настройки защиты сессии")
            manager = self.runtime.config_manager
            with manager._lock:
                cfg = manager.snapshot()
                draft = dict(cfg["download_guard"], **network)
                if (
                    self.downloads.lease
                    or self.downloads.worker
                    and self.downloads.worker.is_alive()
                ) and (
                    self.downloads.scope(draft) != self.downloads.scope(cfg["download_guard"])
                    or values.get("password")
                ):
                    raise ValueError(
                        "Сначала закончите матч и дождитесь возврата скорости; затем меняйте подключение"
                    )
                cfg["resource_guard"].update(resource)
                cfg["download_guard"].update(network)
                if values.get("password"):
                    if not isinstance(values["password"], str) or len(values["password"]) > 1000:
                        raise ValueError("Ключ доступа слишком длинный")
                    key = (
                        "qbittorrent_password"
                        if draft["client"] == "qbittorrent"
                        else "aria2_secret"
                    )
                    cfg["api_keys"][key] = values["password"]
                manager.save_config(cfg)
            self.downloads._settled = None
            self.next_tick = 0
            self.tick()
            return "Настройки защиты сохранены"
        if action == "game.install":
            result = self.phase.install(str(values.get("appid", "")))
            manager = self.runtime.config_manager
            with manager._lock:
                cfg = manager.snapshot()
                cfg["download_guard"]["gsi"] = True
                manager.save_config(cfg)
            return result
        if action == "game.launch":
            if os.name != "nt":
                raise ValueError("Запуск игры доступен в Windows")
            appid = str(values.get("appid", ""))
            if appid not in ("570", "730"):
                raise ValueError("Выберите Dota 2 или CS2")
            os.startfile(
                "steam://run/" + appid + ("//-gamestateintegration/" if appid == "570" else "")
            )
            return "Запуск передан Steam"
        raise ValueError("Неизвестное действие защиты сессии")

    def close(self):
        self.downloads.close()
        self.steam.close()
