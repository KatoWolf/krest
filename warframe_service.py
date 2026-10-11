"""Personal Warframe companion: local notebook, public data, bounded background tasks."""

import copy
import os
from pathlib import Path
import queue
import re
import threading
import time
import uuid
import psutil

from warframe_api import (
    DataCache,
    ItemCatalog,
    WORLD_BASE,
    MARKET_BASE,
    cycle_rows,
    event_rows,
    world_briefing,
    timestamp,
    local_time,
    remaining,
    market_items,
    normalize_orders,
    item_query,
)
from warframe_store import WarframeStore
from warframe_logic import (
    session_plan,
    task_done,
    next_reset,
    trace_cost,
    build_report,
    parse_mods,
    resource_advice,
    wiki_url,
)
from game_capture import GAME_PROCESSES


class WarframeService:
    def __init__(self, runtime, root, cache=None):
        self.runtime = runtime
        self.root = Path(root)
        self.store = WarframeStore(self.root / "warframe_state.json")
        self.cache = cache or DataCache(self.root / "warframe_cache")
        self.catalog = ItemCatalog(self.cache)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.thread = None
        self.running = False
        self.started = 0
        self.last_world = None
        self.error = ""
        self.busy = 0
        self.operation = ""
        self._last_detect = self._last_poll = self._last_health = self._last_local = 0
        self._watch_due = {}
        self._profile = None
        self._volumes = []
        self._last_error = ""
        self._tasks = {}
        self._queue = queue.Queue(maxsize=4)
        self._workers = []
        self._world_lock = threading.Lock()
        self._last_notice = 0
        self._last_notice_text = ""
        self.panel = {}
        self._hint_screen = ""
        self._network_thread = None
        from game_routes import RouteController

        self.routes = RouteController(
            self.root,
            allowed=lambda: (
                self.cfg().get("route_enabled", False)
                and self.cfg()["enabled"]
                and not self.runtime.settings().get("safe_mode")
            ),
        )
        from game_vision import GameVision

        self.vision = GameVision(
            self.store,
            allowed=lambda: (
                self.running
                and self.cfg().get("vision_enabled", True)
                and self.cfg()["enabled"]
                and not self.runtime.settings().get("safe_mode")
            ),
        )

    def cfg(self):
        from warframe_config import DEFAULT_WARFRAME

        return self.runtime.settings().get("warframe", DEFAULT_WARFRAME)

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self._loop, name="krest-warframe", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        self.routes.close()
        self.vision.close()
        if self.thread:
            self.thread.join(timeout=1)
        self._restore_profile()

    def status(self):
        with self.lock:
            return {
                "running": self.running,
                "panel": copy.deepcopy(self.panel)
                if self.panel.get("until", 0) > time.time()
                else {},
                "vision": self.vision.snapshot(),
                "route": self.routes.snapshot(),
                "started": self.started,
                "busy": bool(self.busy),
                "operation": self.operation,
                "error": self.error or self.store.error,
                "updated": self.last_world["updated"] if self.last_world else 0,
                "stale": (
                    self.last_world["stale"]
                    or time.time() - self.last_world["updated"] > self.cfg()["poll_seconds"]
                )
                if self.last_world
                else True,
                "notice_at": self._last_notice,
                "notice": self._last_notice_text,
            }

    def snapshot(self):
        data = self.store.snapshot()
        now = time.time()
        for row in data["tasks"]:
            row["done_now"] = task_done(row, now)
            row["next_reset"] = next_reset(row, now)
        world = copy.deepcopy(self.last_world) if self.last_world else None
        return {
            "status": self.status(),
            "state": data,
            "cycles": cycle_rows(world["data"], now) if world else [],
            "events": event_rows(world["data"], now) if world else [],
            "baro": (world["data"].get("voidTrader") or {}) if world else {},
            "source": world["source"] if world else WORLD_BASE + self.cfg()["platform"],
            "catalogs": copy.deepcopy(self.catalog.info),
        }

    def request(self, action, args):
        if action not in ACTIONS:
            raise ValueError("Неизвестная операция Warframe")
        with self.lock:
            if not self._workers:
                for i in range(2):
                    worker = threading.Thread(
                        target=self._worker, name=f"krest-warframe-request-{i}", daemon=True
                    )
                    worker.start()
                    self._workers.append(worker)
            if self._queue.full():
                raise ValueError("Очередь Warframe занята. Дождитесь текущего запроса.")
            for key in list(self._tasks):
                if len(self._tasks) <= 20:
                    break
                if self._tasks[key]["status"] != "running":
                    del self._tasks[key]
            ident = uuid.uuid4().hex
            self._tasks[ident] = {"status": "running"}
            self._queue.put_nowait((ident, action, copy.deepcopy(args)))
            return ident

    def result(self, ident):
        with self.lock:
            if ident not in self._tasks:
                raise ValueError("Результат уже удалён; повторите запрос")
            return copy.deepcopy(self._tasks[ident])

    def _worker(self):
        while not self.stop.is_set():
            try:
                ident, action, args = self._queue.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                result = self.action(action, **args)
                with self.lock:
                    self._tasks[ident] = {"status": "completed", "result": result}
            except Exception as exc:
                with self.lock:
                    self._tasks[ident] = {"status": "failed", "error": str(exc)}
            finally:
                self._queue.task_done()

    def notice(self, text, key, priority="normal", repeat_after=None):
        if self.stop.is_set():
            return False
        if self.store.remember_notice(key, repeat_after=repeat_after):
            self.runtime.publish_notice(text, "Warframe", priority)
            self._last_notice = time.time()
            self._last_notice_text = text
            return True
        return False

    def world(self, force=False):
        platform = self.cfg()["platform"]
        with self._world_lock:
            result = self.cache.get(
                "world-" + platform,
                WORLD_BASE + platform + "?language=ru",
                ttl=self.cfg()["poll_seconds"],
                force=force,
                validator=lambda d: (
                    isinstance(d, dict)
                    and any(k in d for k in ("timestamp", "cetusCycle", "fissures", "voidTrader"))
                ),
            )
            source_time = timestamp(result["data"].get("timestamp"))
            if source_time and abs(time.time() - source_time) > 900:
                result["stale"] = True
                result["error"] = "Источник вернул устаревшее время мира: " + local_time(
                    source_time
                )
            self.last_world = result
            self.error = result["error"]
            return result

    def _loop(self):
        while not self.stop.wait(2):
            try:
                self.tick()
            except Exception as exc:
                self.error = str(exc)
                # No repeated toast loop when an API or notebook is unavailable.

    def detect(self):
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").casefold() in GAME_PROCESSES:
                    return True
            except psutil.Error:
                continue
        return False

    def tick(self, now=None):
        now = time.time() if now is None else now
        cfg = self.cfg()
        confirmed = self.vision.confirmed
        if confirmed and confirmed != self._hint_screen:
            self._hint_screen = confirmed
            if (
                confirmed != "unknown"
                and cfg.get("context_hints", True)
                and self.panel.get("until", 0) < now
            ):
                self.show_panel(
                    "Подсказка по экрану",
                    [self.guide_hint(), "«Что мне делать в игре» — повторить"],
                    seconds=12,
                )
        if self.runtime.settings().get("safe_mode") or not cfg["enabled"]:
            self.vision.tick(False, False, now)
            if self.running:
                self.running = False
                self._restore_profile()
            return
        if now - self._last_detect >= 5:
            self._last_detect = now
            running = self.detect()
            if running != self.running:
                self.running = running
                if running:
                    self._on_start(now)
                else:
                    self._on_stop(now)
        self.vision.tick(cfg.get("vision_enabled", True), self.running, now)
        if now - self._last_local >= 5:
            self._last_local = now
            self._local_notices(now)
        network_due = (self.running and now - self._last_poll >= cfg["poll_seconds"]) or (
            cfg["market_notify"]
            and any(
                r.get("enabled", True) and now >= self._watch_due.get(r["id"], 0)
                for r in self.store.snapshot()["watches"]
            )
        )
        if network_due and (not self._network_thread or not self._network_thread.is_alive()):
            self._network_thread = threading.Thread(
                target=self._network_tick,
                args=(now,),
                name="krest-warframe-public-data",
                daemon=True,
            )
            self._network_thread.start()
        if self.running and cfg["health_notify"] and now - self._last_health >= 30:
            self._last_health = now
            self._health(now)

    def _network_tick(self, now):
        cfg = self.cfg()
        if self.running and now - self._last_poll >= cfg["poll_seconds"]:
            self._last_poll = now
            try:
                result = self.action("refresh")
                if (
                    cfg["auto_summary"]
                    and now - self.started < cfg["poll_seconds"] + 10
                    and not result.get("stale")
                ):
                    self.notice(result["text"], "session-summary-" + str(int(self.started)))
                self._world_notices(now)
            except Exception as exc:
                self.error = str(exc)
                if self.error != self._last_error:
                    self._last_error = self.error
                    self.runtime.publish_notice(
                        "Warframe: данные мира пока недоступны. Личные цели и таймеры продолжают работать.",
                        "Warframe",
                        "low",
                        speak=False,
                    )
        if cfg["market_notify"]:
            for row in self.store.snapshot()["watches"]:
                if not row.get("enabled", True) or now < self._watch_due.get(row["id"], 0):
                    continue
                self._watch_due[row["id"]] = now + max(120, cfg["poll_seconds"])
                try:
                    result = self.action("orders", slug=row["slug"], rank=row.get("rank"))
                    if (
                        not result["stale"]
                        and result["rows"]
                        and result["rows"][0]["price"] <= row["price"]
                    ):
                        cheapest = result["rows"][0]
                        self.notice(
                            f"Warframe Market: {row['name']} — {cheapest['price']} платины, {cheapest['seller']}.",
                            f"price:{row['id']}",
                            repeat_after=1800,
                        )
                except Exception as exc:
                    self.error = str(exc)
                break

    def _on_start(self, now):
        self.started = now
        self._last_poll = 0
        self._last_error = ""
        self.store.add("journal", "Начало сессии", notes="Warframe обнаружен в списке процессов")
        cfg = self.cfg()
        if cfg["auto_profile"]:
            self._profile = (self.runtime.performance.override, self.runtime.notification_mode)
            self.runtime.performance.set_mode("game")
            if self.runtime.notification_mode != "silent":
                self.runtime.notification_mode = "important"
            if cfg["volume_apps"] and os.name == "nt":
                try:
                    from windows_audio import sessions, set_app_volume

                    names = {
                        n.strip().casefold() for n in cfg["volume_apps"].split(",") if n.strip()
                    }
                    for row in sessions():
                        if row["name"].casefold() in names:
                            set_app_volume(row["name"], cfg["volume_percent"])
                            self._volumes.append(
                                (row["name"], row["volume"], cfg["volume_percent"])
                            )
                except Exception as exc:
                    self.error = "Микшер: " + str(exc)
        if cfg["start_scenario"]:
            try:
                self.runtime.scenarios.start(cfg["start_scenario"])
            except ValueError as exc:
                self.runtime.publish_notice(str(exc), "Warframe", "low", speak=False)
        pending = [r["name"] for r in self.store.snapshot()["goals"] if not r.get("done")]
        if pending and cfg["auto_summary"]:
            self.notice(
                "Warframe запущен. Ваши цели: " + ", ".join(pending[:3]),
                "session-goals-" + str(int(now)),
            )

    def _restore_profile(self):
        if self._profile:
            performance, notifications = self._profile
            self._profile = None
            if self.runtime.performance.override == "game":
                self.runtime.performance.override = performance
                self.runtime.performance.tick(force=True)
            if self.runtime.notification_mode == "important":
                self.runtime.notification_mode = notifications
        if self._volumes:
            try:
                from windows_audio import sessions, set_app_volume

                current = sessions()
                for name, old, applied in self._volumes:
                    matches = [r for r in current if r["name"] == name]
                    if len(matches) == 1 and matches[0]["volume"] == applied:
                        set_app_volume(name, old)
            except Exception as exc:
                self.error = "Возврат микшера: " + str(exc)
            self._volumes = []

    def _on_stop(self, now):
        self._restore_profile()
        self.store.add(
            "journal", "Конец сессии", notes="Продолжительность: " + remaining(now - self.started)
        )
        self.started = 0

    def _local_notices(self, now):
        data = self.store.snapshot()
        for row in data["foundry"]:
            if not row.get("claimed") and row["ends"] <= now:
                self.notice(
                    "Кузница: расчётное время изготовления «"
                    + row["name"]
                    + "» завершилось. Проверьте предмет в игре.",
                    "foundry:" + row["id"],
                    "high",
                )
        if self.cfg()["reset_notify"]:
            for row in data["tasks"]:
                reset = next_reset(row, now)
                if (
                    reset
                    and 0 < reset - now <= self.cfg()["notify_minutes"] * 60
                    and not task_done(row, now)
                ):
                    self.notice(
                        "Warframe: скоро сброс задачи «" + row["name"] + "».",
                        "task-reset:" + row["id"] + ":" + str(int(reset)),
                    )

    def _world_notices(self, now):
        cfg = self.cfg()
        world = self.last_world
        if not world or world["stale"] or not cfg["notifications"]:
            return
        for row in cycle_rows(world["data"], now):
            if row["id"] in cfg["cycles"] and 0 < row["expiry"] - now <= cfg["notify_minutes"] * 60:
                self.notice(
                    row["name"] + ": смена фазы через " + remaining(row["expiry"] - now) + ".",
                    "cycle:" + row["id"] + ":" + str(row["expiry"]),
                )
        baro = world["data"].get("voidTrader") or {}
        if cfg["baro_notify"] and baro:
            start, end = timestamp(baro.get("activation")), timestamp(baro.get("expiry"))
            if start <= now < end:
                self.notice(
                    "Баро Ки’Тиир прибыл: "
                    + str(baro.get("location", ""))
                    + ". Ассортимент доступен в меню Warframe.",
                    "baro:" + str(start),
                )
                if end - now <= cfg["notify_minutes"] * 60:
                    self.notice("Баро скоро улетает.", "baro-end:" + str(end))
            elif 0 < start - now <= cfg["notify_minutes"] * 60:
                self.notice("Баро скоро прибывает.", "baro-soon:" + str(start))
        words = [v.strip().casefold() for v in cfg["reward_keywords"].split(",") if v.strip()]
        count = 0
        for row in event_rows(world["data"], now):
            if words and any(v in (row["name"] + " " + row["reward"]).casefold() for v in words):
                if self.notice(
                    row["kind"] + ": " + row["name"] + " · " + row["reward"], "event:" + row["id"]
                ):
                    count += 1
                if count >= 3:
                    break

    def _health(self, now):
        cfg = self.cfg()
        bucket = str(int(now // 600))
        if psutil.virtual_memory().percent >= cfg["memory_limit_percent"]:
            self.notice(
                "Warframe: оперативная память почти заполнена. Проверьте фоновые программы.",
                "health-memory:" + bucket,
            )
        free = psutil.disk_usage(str(self.root)).free / 1024**3
        if free < cfg["disk_free_gb"]:
            self.notice(
                f"Warframe: на диске Крест осталось {free:.1f} ГБ.", "health-disk:" + bucket
            )
        probe = getattr(psutil, "sensors_temperatures", None)
        if probe:
            try:
                values = [
                    r.current
                    for group in probe().values()
                    for r in group
                    if isinstance(r.current, (int, float))
                ]
                if values and max(values) >= cfg["temperature_limit"]:
                    self.notice(
                        "Высокая температура доступного датчика: "
                        + str(round(max(values)))
                        + " °C.",
                        "health-temperature:" + bucket,
                    )
            except (OSError, RuntimeError):
                pass

    def orders(self, slug, rank=None):
        if not re.fullmatch("[a-z0-9_]{1,160}", str(slug)):
            raise ValueError("Выберите предмет из поиска рынка")
        platform = {"pc": "pc", "ps4": "ps4", "xb1": "xbox", "swi": "switch"}[
            self.cfg()["platform"]
        ]
        result = self.cache.get(
            "orders-" + platform + "-" + slug,
            MARKET_BASE + "orders/item/" + slug,
            ttl=120,
            validator=lambda d: isinstance(d, dict),
            headers={"Platform": platform},
        )
        if rank is not None and str(rank) not in ("", "Любой"):
            rank = int(rank)
            if not 0 <= rank <= 10:
                raise ValueError("Ранг: 0–10")
        else:
            rank = None
        rows = normalize_orders(result["data"], self.cfg()["market_online_only"], rank=rank)
        text = (
            "\n".join(f"{r['price']} пл. · {r['seller']} · {r['status']}" for r in rows[:10])
            or "Нет подходящих предложений."
        )
        if result["stale"]:
            text = "Сохранённые предложения — актуальность не подтверждена.\n" + text
        return {
            "rows": rows,
            "updated": result["updated"],
            "stale": result["stale"],
            "error": result["error"],
            "text": text,
        }

    def action(self, action, **args):
        if action not in ACTIONS:
            raise ValueError("Неизвестная операция Warframe")
        with self.lock:
            self.busy += 1
            self.operation = ACTION_NAMES.get(action, "Работа с Warframe")
        try:
            return self._action(action, **args)
        except Exception as exc:
            self.error = str(exc)
            raise
        finally:
            with self.lock:
                self.busy -= 1
                self.operation = "" if not self.busy else self.operation

    def show_panel(self, title, lines, seconds=90, stale=False):
        with self.lock:
            self.panel = {
                "title": title[:60],
                "lines": [str(line)[:130] for line in lines[:6]],
                "until": time.time() + seconds,
                "stale": bool(stale),
            }

    def guide_hint(self):
        from game_vision import screen_hint

        vision = self.vision.snapshot()
        fresh = time.time() - vision.get("observed", 0) < 40
        return screen_hint(vision.get("screen", "unknown") if fresh else "unknown")

    def _action(self, action, **args):
        state = self.store.snapshot()
        aliases = state["aliases"]
        if action.startswith("route_"):
            methods = {
                "route_record": self.routes.record,
                "route_save": self.routes.cancel,
                "route_request": self.routes.request,
                "route_confirm": self.routes.confirm,
                "route_stop": self.routes.cancel,
            }
            text = methods[action]()
            self.show_panel(
                "Маршрут · под вашим контролем",
                [text, "Esc / F8 / «верни управление» — остановка"],
                seconds=20,
            )
            return {"text": text}
        if action == "hide_panel":
            self.panel = {}
            return {"text": "Сводка скрыта."}
        if action == "guide":
            hint = self.guide_hint()
            self.show_panel(
                "Подсказка Крест",
                [hint, "«Открой сводку по миру» — циклы и цели", "«Скрой сводку» — свернуть"],
            )
            return {"text": hint}
        if action == "show_briefing":
            try:
                world = self.world()
            except Exception:
                world = self.last_world
            lines = []
            stale = (
                not world
                or world.get("stale", True)
                or time.time() - world.get("updated", 0) > self.cfg()["poll_seconds"]
            )
            if world:
                lines += [
                    r["name"] + ": " + r["state"] + " · " + r["remaining"]
                    for r in cycle_rows(world["data"])
                ][:3]
                events = event_rows(world["data"])
                if events:
                    row = events[0]
                    lines.append(row["kind"] + ": " + row["name"])
            else:
                lines.append("Нет связи с источником мира. Личные цели доступны.")
            goal = next((g for g in state["goals"] if not g.get("done")), None)
            if goal:
                lines.append("Ваша цель: " + goal["name"])
            lines.append(self.guide_hint())
            self.show_panel("Мир Warframe", lines, stale=stale)
            return {
                "text": "Сводка открыта под крестом на 90 секунд."
                + (" Данные мира не подтверждены." if stale else ""),
                "stale": stale,
            }
        if action == "vision":
            return {"text": self.vision.snapshot()["status"]}
        if action == "snapshot":
            return self.snapshot()
        if action in ("refresh", "briefing", "cycles", "events", "baro", "nightwave"):
            world = self.world(force=action == "refresh")
            data = world["data"]
            if action in ("refresh", "briefing"):
                return {"text": world_briefing(world, state["goals"]), "stale": world["stale"]}
            if action == "cycles":
                rows = cycle_rows(data)
                return {
                    "text": ("Сохранённые данные.\n" if world["stale"] else "")
                    + "\n".join(
                        r["name"] + ": " + r["state"] + " · " + r["remaining"] for r in rows
                    ),
                    "rows": rows,
                }
            if action in ("events", "nightwave"):
                rows = event_rows(data)
                if action == "nightwave":
                    rows = [r for r in rows if r["kind"] == "Ночная Волна"]
                groups = {}
                for row in rows:
                    for tag in (
                        "убий",
                        "kill",
                        "выжив",
                        "surviv",
                        "мисси",
                        "mission",
                        "реликв",
                        "relic",
                    ):
                        if tag in row["name"].casefold():
                            groups.setdefault(tag, []).append(row["name"])
                combinations = [values for values in groups.values() if len(values) > 1]
                text = (
                    "\n".join(r["kind"] + ": " + r["name"] + " · " + r["reward"] for r in rows[:30])
                    or "В источнике нет активных событий этого типа."
                )
                if action == "nightwave" and combinations:
                    text += (
                        "\nПохожие по формулировке задачи, которые стоит проверить на совместимость:\n"
                        + "\n".join(" / ".join(v[:3]) for v in combinations[:3])
                    )
                return {
                    "text": ("Сохранённые данные.\n" if world["stale"] else "") + text,
                    "rows": rows,
                }
            baro = data.get("voidTrader") or {}
            inventory = baro.get("inventory", [])
            desired = {item_query(r["name"], aliases) for r in state["goals"] if not r.get("done")}
            shopping = [r for r in inventory if item_query(r.get("item", ""), aliases) in desired]
            ducats = sum(int(r.get("ducats", 0)) for r in shopping)
            credits = sum(int(r.get("credits", 0)) for r in shopping)
            text = (
                "Баро: "
                + str(baro.get("location", "место не указано"))
                + "\nПрибытие "
                + local_time(baro.get("activation"))
                + "; отъезд "
                + local_time(baro.get("expiry"))
                + "\n"
            )
            text += (
                "\n".join(
                    str(r.get("item", ""))
                    + f" · {r.get('ducats', 0)} дукатов · {r.get('credits', 0)} кредитов"
                    for r in inventory[:100]
                )
                or "Ассортимент пока не опубликован."
            )
            text += f"\nСовпадения с вашими целями: {len(shopping)}. Нужно {ducats} дукатов и {credits} кредитов. Не хватает по вашей записи: {max(0, ducats - state['inventory'].get('ducats', 0))} дукатов, {max(0, credits - state['inventory'].get('credits', 0))} кредитов."
            return {
                "text": ("Сохранённые данные.\n" if world["stale"] else "") + text,
                "rows": inventory,
                "shopping": shopping,
            }
        if action == "add_goal":
            minutes = int(args.get("minutes", 15))
            priority = int(args.get("priority", 2))
            target = int(args.get("target", 1))
            if not 1 <= minutes <= 480 or not 1 <= priority <= 5 or not 1 <= target <= 10**9:
                raise ValueError("Время 1–480 мин, приоритет 1–5, количество от 1")
            row = self.store.add(
                "goals",
                args["name"],
                notes=str(args.get("notes", "")),
                minutes=minutes,
                priority=priority,
                target=target,
                progress=0,
                done=False,
            )
            return {"text": "Цель добавлена: " + row["name"], "row": row}
        if action in ("edit_goal", "edit_task"):
            section = "goals" if action == "edit_goal" else "tasks"
            row = next((r for r in state[section] if r["id"] == args.get("id")), None)
            if row is None:
                raise ValueError("Запись уже удалена")
            values = {
                "name": str(args.get("name", row["name"])).strip(),
                "minutes": int(args.get("minutes", row.get("minutes", 15))),
            }
            if not 1 <= values["minutes"] <= 480:
                raise ValueError("Длительность: 1–480 минут")
            if section == "goals":
                values.update(
                    target=int(args.get("target", row.get("target", 1))),
                    priority=int(args.get("priority", row.get("priority", 2))),
                )
                if not 1 <= values["target"] <= 10**9 or not 1 <= values["priority"] <= 5:
                    raise ValueError("Количество от 1, приоритет 1–5")
                values["progress"] = min(row.get("progress", 0), values["target"])
                values["done"] = values["progress"] >= values["target"]
            else:
                values.update(
                    period=args.get("period", row["period"]),
                    reset_hour=int(args.get("reset_hour", row.get("reset_hour", 0))),
                )
                if values["period"] != row["period"] or values["reset_hour"] != row.get(
                    "reset_hour", 0
                ):
                    values.update(done=False, completed_at=0)
            self.store.update(section, args["id"], **values)
            return {"text": "Изменения сохранены"}
        if action == "progress":
            section = args.get("section", "goals")
            ident = args["id"]
            value = int(args.get("value", 1))
            if section != "goals":
                raise ValueError("Прогресс доступен для целей")
            row = next((r for r in state["goals"] if r["id"] == ident), None)
            if not row:
                raise ValueError("Цель не найдена")
            if not 0 <= value <= row.get("target", 1):
                raise ValueError("Прогресс не должен превышать нужное количество")
            self.store.update("goals", ident, progress=value, done=value >= row.get("target", 1))
            return {"text": "Прогресс сохранён"}
        if action == "add_task":
            minutes = int(args.get("minutes", 15))
            hour = int(args.get("reset_hour", 0))
            if not 1 <= minutes <= 480 or not 0 <= hour <= 23:
                raise ValueError("Проверьте длительность и час сброса UTC")
            row = self.store.add(
                "tasks",
                args["name"],
                notes=str(args.get("notes", "")),
                period=args.get("period", "daily"),
                minutes=minutes,
                priority=2,
                reset_hour=hour,
                reset_weekday=0,
                completed_at=0,
                done=False,
            )
            return {"text": "Задача сохранена: " + row["name"]}
        if action == "complete_task":
            self.store.update(
                "tasks",
                args["id"],
                done=args.get("completed", True),
                completed_at=time.time() if args.get("completed", True) else 0,
            )
            return {"text": "Задача отмечена выполненной"}
        if action == "plan":
            return session_plan(state, args.get("minutes", 30))
        if action == "goals":
            return {
                "text": "\n".join(
                    f"{r['name']}: {r.get('progress', 0)}/{r.get('target', 1)}"
                    for r in state["goals"]
                    if not r.get("done")
                )
                or "Пока нет незавершённых целей."
            }
        if action == "remove":
            self.store.remove(args["section"], args["id"])
            return {"text": "Запись удалена"}
        if action == "collection":
            name = item_query(str(args["name"]), aliases)
            value = int(args["quantity"])
            group = "mod_ranks" if args.get("mod") else "inventory"
            with self.store.edit() as data:
                data[group][name] = value
            return {"text": "Личная коллекция обновлена. Это ваша отметка, без чтения аккаунта."}
        if action == "alias":
            with self.store.edit() as data:
                data["aliases"][str(args["name"]).strip().casefold()] = str(args["value"]).strip()
            return {"text": "Название запомнила"}
        if action == "search":
            category = args.get("category", "Warframes")
            rows = self.catalog.search(args["query"], category, aliases)
            return {
                "rows": rows,
                "info": self.catalog.info[category],
                "text": "\n".join(r["name"] for r in rows[:30])
                or "Предмет не найден. Попробуйте английское название или задайте своё соответствие.",
            }
        if action == "refresh_catalog":
            category = args.get("category", "Warframes")
            rows = self.catalog.load(category, force=True)
            return {
                "text": f"Справочник {category}: {len(rows)} записей",
                "info": self.catalog.info[category],
            }
        if action in ("recipe", "farm"):
            query = args["query"]
            category = args.get("category", "Warframes")
            advice = resource_advice(query, aliases) if action == "farm" else ""
            if advice:
                return {"text": advice, "url": wiki_url(item_query(query, aliases))}
            item = self.catalog.exact(query, category, aliases)
            self.catalog.load("Misc")
            self.catalog.load("Resources")
            by_id = {
                r["uniqueName"]: r["name"]
                for group in self.catalog.groups.values()
                for r in group
                if "uniqueName" in r
            }
            rows = []
            for part in item.get("components", []):
                name = (
                    part.get("name")
                    or by_id.get(part.get("uniqueName"))
                    or re.sub(
                        r"(?<=[a-z])(?=[A-Z])", " ", part.get("uniqueName", "").rsplit("/", 1)[-1]
                    )
                )
                needed = int(part.get("itemCount", 1))
                owned = state["inventory"].get(name.casefold(), 0)
                rows.append(
                    {
                        "name": name,
                        "needed": needed,
                        "owned": owned,
                        "missing": max(0, needed - owned),
                    }
                )
            text = item["name"] + "\n" + str(item.get("description", "")) + "\n"
            if rows:
                text += "Состав по справочнику (наличие — по вашим отметкам):\n" + "\n".join(
                    f"{r['name']}: {r['owned']}/{r['needed']}, не хватает {r['missing']}"
                    for r in rows
                )
            else:
                text += "Рецепт в источнике отсутствует.\n"
            if item.get("buildTime"):
                text += (
                    "\nИзготовление: "
                    + remaining(item["buildTime"])
                    + f"; кредиты: {item.get('buildPrice', 0)}."
                )
            locations = item.get("locations", item.get("drops", []))
            if locations:
                text += "\nИсточники: " + str(locations)[:3000]
            text += "\nПорядок: проверьте доступность чертежа, соберите недостающие компоненты, затем запустите изготовление."
            return {
                "text": text,
                "rows": rows,
                "item": item,
                "url": wiki_url(item["name"]),
                "info": copy.deepcopy(self.catalog.info[category]),
            }
        if action == "relics":
            rows = self.catalog.relics_for(args["query"], aliases)
            text = (
                "\n".join(
                    f"{r['relic']}: {r['item']} · {r['chance']}% · "
                    + (
                        "в хранилище"
                        if r["vaulted"] is True
                        else "доступность проверяйте по источникам"
                        if r["vaulted"] is None
                        else "не в хранилище"
                    )
                    for r in rows[:35]
                )
                or "Подходящие реликвии не найдены."
            )
            return {"text": text, "rows": rows, "info": self.catalog.info["Relics"]}
        if action == "traces":
            return trace_cost(
                args.get("tier", "radiant"), args.get("count", 1), args.get("owned", 0)
            )
        if action == "foundry_start":
            name = args["name"]
            seconds = args.get("seconds")
            if seconds is None:
                seconds = self.catalog.exact(name, args.get("category", "Misc"), aliases).get(
                    "buildTime"
                )
            if not isinstance(seconds, (float, int)) or not 1 <= seconds <= 30 * 86400:
                raise ValueError(
                    "Неизвестная длительность. Укажите её вручную: от 1 секунды до 30 дней."
                )
            row = self.store.add("foundry", name, ends=time.time() + seconds, claimed=False)
            return {
                "text": "Локальный таймер Кузницы: "
                + name
                + ", до "
                + local_time(row["ends"])
                + ". Начало изготовления в игре отметьте самостоятельно.",
                "row": row,
            }
        if action == "foundry_claim":
            self.store.update("foundry", args["id"], claimed=True)
            return {"text": "Предмет отмечен полученным"}
        if action == "foundry":
            return {
                "text": "\n".join(
                    r["name"]
                    + " · "
                    + (
                        "расчётное время вышло"
                        if r["ends"] <= time.time()
                        else remaining(r["ends"] - time.time())
                    )
                    for r in state["foundry"]
                    if not r.get("claimed")
                )
                or "Кузница: активных локальных таймеров нет."
            }
        if action == "save_build":
            mods = parse_mods(args.get("mods", ""))
            values = dict(
                name=str(args["name"]).strip(),
                mods=mods,
                notes=str(args.get("notes", "")),
                url=str(args.get("url", "")),
            )
            if any(
                r["name"].casefold() == values["name"].casefold() and r["id"] != args.get("id")
                for r in state["builds"]
            ):
                raise ValueError("Такая сборка уже есть. Выберите её и нажмите «Редактировать».")
            if args.get("id"):
                row = self.store.update("builds", args["id"], **values)
            else:
                row = self.store.add("builds", **values)
            return {"text": "Билд сохранён: " + row["name"], "row": row}
        if action == "build_report":
            row = next(
                (
                    r
                    for r in state["builds"]
                    if r["id"] == args.get("id")
                    or r["name"].casefold() == str(args.get("name", "")).casefold()
                ),
                None,
            )
            if not row:
                raise ValueError("Билд не найден")
            return build_report(row, state, self.catalog.load("Mods"))
        if action == "market_search":
            result = self.cache.get(
                "market-items",
                MARKET_BASE + "items",
                ttl=86400,
                validator=lambda d: isinstance(d, dict),
            )
            query = item_query(args["query"], aliases)
            rows = [
                r
                for r in market_items(result["data"])
                if query in (r["name"] + " " + r["ru"] + " " + r["slug"]).casefold()
            ][:60]
            return {
                "rows": rows,
                "text": "\n".join(r["name"] for r in rows) or "Предмет рынка не найден",
                "updated": result["updated"],
                "stale": result["stale"],
            }
        if action == "orders":
            return self.orders(args["slug"], args.get("rank"))
        if action == "watch_price":
            row = self.store.add(
                "watches",
                args["name"],
                slug=args["slug"],
                price=int(args["price"]),
                rank=args.get("rank"),
                enabled=True,
            )
            return {"text": "Слежение за ценой включено: " + row["name"]}
        if action == "watch_toggle":
            self.store.update("watches", args["id"], enabled=bool(args["enabled"]))
            return {"text": "Правило цены обновлено"}
        if action == "trade":
            kind = args.get("kind", "sell")
            price = int(args["price"])
            quantity = int(args.get("quantity", 1))
            if (
                kind not in ("sell", "buy")
                or not 0 <= price <= 1000000
                or not 1 <= quantity <= 10000
            ):
                raise ValueError("Некорректные параметры сделки")
            self.store.add(
                "trades",
                args["name"],
                kind=kind,
                price=price,
                quantity=quantity,
                notes=str(args.get("notes", "")),
            )
            net = sum(
                r.get("price", 0) * r.get("quantity", 1) * (1 if r.get("kind") == "sell" else -1)
                for r in self.store.snapshot()["trades"]
            )
            return {"text": f"Сделка записана вручную. Баланс записанных сделок: {net} платины."}
        if action == "bookmark":
            name = str(args.get("name", "Игровой момент"))
            notes = ""
            cfg = self.cfg()
            if cfg["capture_on_bookmark"]:
                try:
                    from game_capture import save_screenshot

                    notes += "Скриншот: " + save_screenshot(self.root)
                except Exception as exc:
                    notes += "Скриншот не сделан: " + str(exc)
            if cfg["clip_on_bookmark"]:
                try:
                    from game_capture import request_clip

                    notes += "\n" + request_clip()
                except Exception as exc:
                    notes += "\nКлип не запрошен: " + str(exc)
            row = self.store.add("journal", name, notes=notes)
            return {"text": "Метка сохранена: " + row["name"] + ("\n" + notes if notes else "")}
        if action == "journal":
            return {
                "text": "\n".join(
                    local_time(r["created"]) + " · " + r["name"] + " " + r.get("notes", "")
                    for r in state["journal"][-10:]
                )
                or "Журнал пока пуст."
            }
        if action == "launch":
            if os.name != "nt":
                raise ValueError("Запуск игры реализован для Windows")
            path = self.cfg()["launch_path"]
            if path:
                path = Path(path).expanduser()
                if not path.is_file() or path.suffix.casefold() not in (".exe", ".lnk"):
                    raise ValueError("Укажите существующий .exe или ярлык игры")
                os.startfile(str(path))
            else:
                os.startfile("steam://run/230410")
            return {
                "text": "Запуск Warframe запрошен. Профиль и сводка включатся после обнаружения процесса игры."
            }
        if action == "export":
            return {"text": "Сохранено: " + self.store.export(args["path"])}
        if action == "import":
            return {"text": self.store.import_file(args["path"])}
        raise ValueError("Действие пока недоступно")


ACTIONS = {
    "route_record",
    "route_save",
    "route_request",
    "route_confirm",
    "route_stop",
    "show_briefing",
    "hide_panel",
    "guide",
    "vision",
    "snapshot",
    "refresh",
    "briefing",
    "cycles",
    "events",
    "baro",
    "nightwave",
    "add_goal",
    "progress",
    "edit_goal",
    "edit_task",
    "add_task",
    "complete_task",
    "plan",
    "goals",
    "remove",
    "collection",
    "alias",
    "search",
    "refresh_catalog",
    "recipe",
    "farm",
    "relics",
    "traces",
    "foundry_start",
    "foundry_claim",
    "foundry",
    "save_build",
    "build_report",
    "market_search",
    "orders",
    "watch_price",
    "watch_toggle",
    "trade",
    "bookmark",
    "journal",
    "launch",
    "export",
    "import",
}
ACTION_NAMES = {
    "refresh": "Обновляю события",
    "market_search": "Ищу предмет на рынке",
    "orders": "Проверяю предложения",
    "search": "Ищу в справочнике",
    "relics": "Подбираю реликвии",
    "recipe": "Проверяю рецепт",
    "farm": "Планирую фарм",
    "bookmark": "Сохраняю игровой момент",
}
