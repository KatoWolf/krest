import copy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import time

import pytest
import requests
from configuration import ConfigManager
from warframe_api import (
    DataCache,
    ItemCatalog,
    WORLD_BASE,
    cycle_rows,
    event_rows,
    normalize_orders,
    market_items,
)
from warframe_store import WarframeStore, empty_state, validate_state
from warframe_logic import (
    task_done,
    next_reset,
    session_plan,
    trace_cost,
    mod_upgrade_cost,
    build_report,
)
from warframe_service import WarframeService
from game_overlay import indicator_state


class Response:
    def __init__(self, value, status=200, headers=None):
        self.value = value
        self.status_code = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def iter_content(self, size):
        yield json.dumps(self.value).encode()


class Cache:
    def __init__(self, values=None):
        self.values = values or {}
        self.calls = []
        self.stale = False

    def get(self, key, url, **kw):
        self.calls.append((key, url, kw))
        if key not in self.values:
            raise RuntimeError("offline")
        return dict(
            data=copy.deepcopy(self.values[key]),
            updated=time.time(),
            stale=self.stale,
            error="offline" if self.stale else "",
            source=url,
        )


@pytest.fixture
def service(tmp_path):
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.config["warframe"]["health_notify"] = False

    class Policy:
        override = None

        def set_mode(self, mode):
            self.override = mode

        def tick(self, force=False):
            pass

    notices = []
    rt = SimpleNamespace(
        settings=cfg.snapshot,
        performance=Policy(),
        notification_mode=None,
        publish_notice=lambda text, *a, **kw: notices.append(text),
        scenarios=SimpleNamespace(start=lambda n: None),
    )
    service = WarframeService(rt, tmp_path, Cache())
    service.test_config = cfg
    service.test_notices = notices
    yield service
    service.close()


def test_cache_fresh_stale_and_bounded_retry(tmp_path):
    calls = []
    clock = [1000]

    def transport(url, **kwargs):
        calls.append(url)
        return Response({"cetusCycle": {}})

    cache = DataCache(tmp_path, transport=transport, clock=lambda: clock[0])
    first = cache.get("world", WORLD_BASE + "pc", ttl=120)
    assert not first["stale"]
    cache.get("world", WORLD_BASE + "pc")
    assert len(calls) == 1
    clock[0] += 121

    def offline(*a, **kw):
        calls.append("offline")
        raise requests.ConnectionError("offline")

    cache.transport = offline
    old = cache.get("world", WORLD_BASE + "pc")
    assert old["stale"] and old["updated"] == 1000 and "offline" in old["error"]
    cache.get("world", WORLD_BASE + "pc", force=True)
    assert len(calls) == 2
    with pytest.raises(ValueError):
        cache.get("x", "https://other.example/test")


def test_cache_rejects_large_or_invalid_payload_and_obeys_429(tmp_path):
    clock = [1000]
    calls = []

    def limited(*a, **kw):
        calls.append(1)
        return Response({}, 429, {"Retry-After": "300"})

    cache = DataCache(tmp_path, transport=limited, clock=lambda: clock[0])
    with pytest.raises(RuntimeError):
        cache.get("w", WORLD_BASE + "pc")
    assert cache.retry["w"] == 1300
    with pytest.raises(RuntimeError):
        cache.get("w", WORLD_BASE + "pc", force=True)
    assert len(calls) == 1
    cache.transport = lambda *a, **kw: Response({"too": "long"})
    with pytest.raises(RuntimeError):
        cache.get("big", WORLD_BASE + "pc", limit=2)
    with pytest.raises(RuntimeError):
        cache.get("bad", WORLD_BASE + "pc", validator=lambda _: False)


def test_world_cycles_never_invent_a_new_phase_and_expired_events_are_removed():
    world = {
        "cetusCycle": {"state": "night", "expiry": 1100},
        "vallisCycle": {"state": "cold", "expiry": 900},
        "fissures": [
            {"id": "old", "expiry": 900},
            {"id": "fresh", "expiry": 1200, "node": "Hepit", "tier": "Lith"},
        ],
        "nightwave": None,
    }
    rows = cycle_rows(world, 1000)
    assert rows[0]["state"] == "ночь" and not rows[0]["expired"]
    assert rows[1]["expired"] and rows[1]["remaining"] == "Нужно обновить"
    assert [r["id"] for r in event_rows(world, 1000)] == ["fresh"]


def test_store_atomic_validation_and_persistence(tmp_path):
    path = tmp_path / "warframe_state.json"
    store = WarframeStore(path)
    goal = store.add("goals", "Форма", target=2, progress=0)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        store.update("goals", goal["id"], progress=float("nan"))
    assert path.read_bytes() == original and store.snapshot()["goals"][0]["progress"] == 0
    assert WarframeStore(path).snapshot()["goals"][0]["name"] == "Форма"
    assert store.remember_notice("unique") and not store.remember_notice("unique")


@pytest.mark.parametrize(
    "values", [{"reset_hour": 25}, {"reset_weekday": 9}, {"completed_at": "oops"}, {"done": "yes"}]
)
def test_import_validates_reset_fields(values):
    data = empty_state()
    data["tasks"] = [dict(id="t", name="Тест", period="daily", **values)]
    with pytest.raises(ValueError):
        validate_state(data)


def test_import_disables_price_rules_and_retains_notebook(tmp_path):
    source = WarframeStore(tmp_path / "a.json")
    source.add("watches", "Forma", slug="forma", price=10, enabled=True)
    other = WarframeStore(tmp_path / "b.json")
    other.import_file(source.path)
    assert other.snapshot()["watches"][0]["enabled"] is False


def test_daily_and_weekly_resets_are_utc_not_local_timezone():
    stamp = lambda text: datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
    sunday = stamp("2026-10-11T23:59:00")
    monday = stamp("2026-10-12T00:00:00")
    weekly = {"period": "weekly", "completed_at": sunday}
    assert task_done(weekly, sunday) and not task_done(weekly, monday)
    assert next_reset(weekly, sunday) == monday
    daily = {"period": "daily", "reset_hour": 5, "completed_at": monday}
    assert task_done(daily, monday + 4 * 3600) and not task_done(daily, monday + 5 * 3600)


def test_plan_only_fits_unfinished_tasks_and_preserves_priorities():
    state = empty_state()
    state["goals"] = [
        {"name": "Большая цель", "minutes": 45, "priority": 5},
        {"name": "Высокий приоритет", "minutes": 20, "priority": 4},
        {"name": "Мелочь", "minutes": 10, "priority": 2},
        {"name": "Готово", "minutes": 5, "done": True},
    ]
    plan = session_plan(state, 30)
    assert [r["name"] for r in plan["rows"]] == ["Высокий приоритет", "Мелочь"] and plan[
        "remaining"
    ] == 0


def test_trace_and_mod_costs_account_for_existing_rank():
    assert trace_cost("radiant", 4, 150)["missing"] == 250
    assert mod_upgrade_cost("legendary", 0, 10) == {"endo": 40920, "credits": 1976436}
    state = empty_state()
    state["mod_ranks"]["vitality"] = 5
    result = build_report(
        {"name": "Тест", "mods": {"Vitality": 10, "Unknown": 5}},
        state,
        [{"name": "Vitality", "rarity": "Common", "fusionLimit": 10}],
    )
    assert (
        result["endo"] == 9920
        and result["missing"] == ["Unknown"]
        and result["unknown"] == ["Unknown"]
    )


def test_recipe_joins_components_and_subtracts_manual_inventory(service):
    service.cache.values.update(
        {
            "items-Misc": [
                {
                    "name": "Forma",
                    "uniqueName": "/Forma",
                    "buildTime": 82800,
                    "components": [{"uniqueName": "/Neurodes", "itemCount": 2}],
                }
            ],
            "items-Resources": [{"name": "Neurodes", "uniqueName": "/Neurodes"}],
        }
    )
    service.action("collection", name="нейроды", quantity=1)
    result = service.action("recipe", query="форма", category="Misc")
    assert result["rows"] == [{"name": "Neurodes", "needed": 2, "owned": 1, "missing": 1}]
    assert "23 ч" in result["text"]
    timer = service.action("foundry_start", name="форма", category="Misc")["row"]
    assert abs(timer["ends"] - time.time() - 82800) < 2


def test_relic_search_by_reward_and_relic_includes_refinement_chances():
    cache = Cache(
        {
            "items-Relics": [
                {
                    "name": "Lith A1 Radiant",
                    "vaulted": True,
                    "rewards": [{"chance": 10, "item": {"name": "Prime Part"}}],
                }
            ]
        }
    )
    catalog = ItemCatalog(cache)
    assert catalog.relics_for("Prime Part")[0]["chance"] == 10
    assert catalog.relics_for("Lith A1")[0]["vaulted"] is True
    with pytest.raises(ValueError):
        catalog.relics_for("")


def test_foundry_offline_notifications_are_exactly_once(service):
    row = service.action("foundry_start", name="Форма", seconds=1)["row"]
    service._local_notices(row["ends"] + 1)
    service._local_notices(row["ends"] + 2)
    assert len(service.test_notices) == 1 and "Проверьте" in service.test_notices[0]
    assert service.cache.calls == []


def test_game_profile_restores_only_unmodified_overrides(service):
    service._on_start(time.time())
    assert service.runtime.performance.override == "game"
    assert service.runtime.notification_mode == "important"
    service._on_stop(time.time() + 60)
    assert (
        service.runtime.performance.override is None and service.runtime.notification_mode is None
    )
    service._on_start(time.time())
    service.runtime.performance.override = "economy"
    service.runtime.notification_mode = "silent"
    service._on_stop(time.time() + 60)
    assert (
        service.runtime.performance.override == "economy"
        and service.runtime.notification_mode == "silent"
    )


def test_baro_and_event_notifications_ignore_stale_data_and_deduplicate(service):
    now = time.time()
    service.cache.values["world-pc"] = {
        "timestamp": now,
        "voidTrader": {"activation": now - 100, "expiry": now + 5000, "location": "Relay"},
        "cetusCycle": {"state": "night", "expiry": now + 60},
    }
    service.world()
    service._world_notices(now)
    service._world_notices(now)
    assert len(service.test_notices) == 2
    service.store.data["notifications"] = {}
    service.last_world["stale"] = True
    service._world_notices(now)
    assert len(service.test_notices) == 2


def test_market_v2_filters_buy_offline_hidden_bad_users_and_rank(service):
    def order(ident, price=10, status="ingame", **extra):
        return dict(
            id=ident,
            type="sell",
            platinum=price,
            quantity=1,
            rank=5,
            user={"ingameName": "Tenno-1", "status": status},
            **extra,
        )

    payload = {
        "data": [
            order("cheap", 10),
            order("higher", 20),
            order("offline", 5, "offline"),
            order("hidden", 5, visible=False),
            dict(order("buy", 3), type="buy"),
            dict(order("bad", 2), user={"ingameName": "x\n/run", "status": "online"}),
        ]
    }
    assert [r["id"] for r in normalize_orders(payload)] == ["cheap", "higher"]
    service.cache.values["orders-pc-vitality"] = payload
    assert len(service.orders("vitality", 5)["rows"]) == 2
    assert service.cache.calls[-1][2]["headers"] == {"Platform": "pc"}
    assert service.orders("vitality", 0)["rows"] == []
    with pytest.raises(ValueError):
        service.orders("../other")
    items = market_items(
        {
            "data": [
                {
                    "slug": "vitality",
                    "i18n": {"en": {"name": "Vitality"}, "ru": {"name": "Живучесть"}},
                }
            ]
        }
    )
    assert items[0]["name"] == "Vitality" and items[0]["ru"] == "Живучесть"


def test_request_queue_finishes_and_records_failure(service):
    ident = service.request("add_goal", {"name": "Моя цель"})
    until = time.monotonic() + 3
    while service.result(ident)["status"] == "running" and time.monotonic() < until:
        time.sleep(0.01)
    assert service.result(ident)["status"] == "completed"
    bad = service.request("progress", {"id": "missing"})
    while service.result(bad)["status"] == "running" and time.monotonic() < until:
        time.sleep(0.01)
    assert service.result(bad)["status"] == "failed"
    assert not service.status()["busy"]
    with pytest.raises(ValueError):
        service.request("__dict__", {})


def test_voice_commands_reach_personal_notebook_and_spoken_duration(service):
    from app_runtime import AssistantRuntime

    rt = AssistantRuntime(service.test_config.snapshot, data_dir=service.root / "voice-profile")
    try:
        result = rt.execute("варфрейм цель собрать некроса")
        assert (
            result.success and rt.warframe.store.snapshot()["goals"][0]["name"] == "собрать некроса"
        )
        result = rt.execute("варфрейм план на тридцать минут")
        assert result.success and "30 минут" in result.text
        result = rt.execute("варфрейм кузница форма на двадцать три часа")
        assert result.success
        assert abs(rt.warframe.store.snapshot()["foundry"][0]["ends"] - time.time() - 23 * 3600) < 3
    finally:
        rt.close()


@pytest.mark.parametrize(
    "data,expected",
    [
        ({}, "idle"),
        ({"game": {"busy": True}}, "busy"),
        ({"voice": {"speaking": True}}, "speaking"),
        ({"listening": True, "voice": {"connected": True, "rms": 500}}, "listening"),
        ({"game": {"error": "offline"}}, "error"),
        ({"game": {"notice_at": 99}}, "notice"),
        ({"voice": {"rms": 500}}, "idle"),
        ({"listening": True, "voice": {"mic_error": "no device"}}, "error"),
    ],
)
def test_indicator_reflects_actual_telemetry(data, expected):
    assert indicator_state(data, now=100) == expected


def test_editing_goals_tasks_and_builds_updates_existing_records(service):
    goal = service.action("add_goal", name="Старая цель", target=3)["row"]
    service.action("progress", id=goal["id"], value=2)
    service.action("edit_goal", id=goal["id"], name="Новая цель", target=1, minutes=20, priority=5)
    row = service.store.snapshot()["goals"][0]
    assert row["name"] == "Новая цель" and row["done"] and row["progress"] == 1
    service.action("add_task", name="Задача", period="daily")
    task = service.store.snapshot()["tasks"][0]
    service.action("complete_task", id=task["id"])
    assert task_done(service.store.snapshot()["tasks"][0])
    service.action("complete_task", id=task["id"], completed=False)
    assert not task_done(service.store.snapshot()["tasks"][0])
    service.action("edit_task", id=task["id"], name="Неделя", period="weekly", reset_hour=4)
    assert service.store.snapshot()["tasks"][0]["reset_hour"] == 4
    build = service.action("save_build", name="Билд", mods="Vitality:5")["row"]
    service.action("save_build", id=build["id"], name="Билд", mods="Vitality:10")
    assert len(service.store.snapshot()["builds"]) == 1
    assert service.store.snapshot()["builds"][0]["mods"] == {"Vitality": 10}
    with pytest.raises(ValueError):
        service.action("save_build", name="Билд")


def test_rank_filter_applies_before_result_limit():
    rows = [
        dict(
            id=str(i),
            type="sell",
            platinum=1,
            quantity=1,
            rank=0,
            user={"ingameName": "Tenno", "status": "online"},
        )
        for i in range(60)
    ]
    rows.append(dict(rows[0], id="wanted", rank=10, platinum=100))
    assert normalize_orders({"data": rows}, rank=10)[0]["id"] == "wanted"


def test_cooldown_after_success_does_not_mark_fresh_data_stale(tmp_path):
    cache = DataCache(
        tmp_path, transport=lambda *a, **kw: Response({"timestamp": 1000}), clock=lambda: 1000
    )
    cache.get("world", WORLD_BASE + "pc")
    result = cache.get("world", WORLD_BASE + "pc", force=True)
    assert not result["stale"] and not result["error"]


def test_slow_public_api_does_not_block_local_foundry_timer(service, monkeypatch):
    import threading

    entered = threading.Event()
    release = threading.Event()

    def get(*a, **kw):
        entered.set()
        release.wait(3)
        raise RuntimeError("offline")

    monkeypatch.setattr(service.cache, "get", get)
    monkeypatch.setattr(service, "detect", lambda: True)
    now = time.time()
    try:
        service.tick(now)
        assert entered.wait(1)
        service.store.add("foundry", "Быстрый таймер", ends=now, claimed=False)
        before = time.monotonic()
        service.tick(now + 6)
        assert time.monotonic() - before < 0.5
        assert any("Быстрый таймер" in message for message in service.test_notices)
    finally:
        release.set()
        if service._network_thread:
            service._network_thread.join(timeout=2)


def test_screenshot_only_uses_verified_foreground_game_bounds(tmp_path, monkeypatch):
    import game_capture
    from PIL import Image, ImageGrab

    boxes = []
    monkeypatch.setattr(game_capture, "foreground_game_rect", lambda: (10, 20, 110, 100))
    monkeypatch.setattr(
        ImageGrab, "grab", lambda **kw: boxes.append(kw) or Image.new("RGB", (100, 80))
    )
    path = game_capture.save_screenshot(tmp_path)
    assert boxes == [{"bbox": (10, 20, 110, 100), "all_screens": True}]
    assert __import__("pathlib").Path(path).is_file()

    def wrong():
        raise ValueError("Сначала переключитесь на окно Warframe")

    monkeypatch.setattr(game_capture, "foreground_game_rect", wrong)
    with pytest.raises(ValueError):
        game_capture.save_screenshot(tmp_path)
    assert len(boxes) == 1


def test_kernel_warframe_rpc_retains_notebook_after_menu_disconnect(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    from kernel_client import KernelClient

    cfg = ConfigManager(tmp_path / "config.json")
    data = cfg.snapshot()
    data["owner_name"] = "Тест"
    data["profile"]["onboarding_completed"] = True
    cfg.save_config(data)
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "main.py"),
            "--kernel",
            "--config",
            str(cfg.filepath),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    client = KernelClient(cfg.filepath)

    def wait(call, predicate):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            try:
                result = call()
                if predicate(result):
                    return result
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        pytest.fail("Kernel did not respond")

    try:
        wait(lambda: client.call("ping"), bool)
        ident = client.call(
            "warframe.request", action="add_goal", values={"name": "Сохранённая цель"}
        )
        result = wait(
            lambda: client.call("warframe.result", id=ident), lambda r: r["status"] != "running"
        )
        assert result["status"] == "completed"
        # A new client sees the same persistent core, with no GUI attached.
        other = KernelClient(cfg.filepath)
        assert other.call("warframe.snapshot")["state"]["goals"][0]["name"] == "Сохранённая цель"
        indicator = other.call("overlay")
        assert indicator["settings"]["overlay_size"] == 52 and not indicator["game"]["running"]
        assert "api_keys" not in indicator
        with pytest.raises(RuntimeError):
            other.call("warframe.request", action="__dict__", values={})
    finally:
        try:
            client.call("shutdown")
        except Exception:
            process.terminate()
        output, error = process.communicate(timeout=15)
        assert process.returncode == 0, output + error
    assert (
        WarframeStore(tmp_path / "warframe_state.json").snapshot()["goals"][0]["name"]
        == "Сохранённая цель"
    )


def test_recent_cache_keeps_failure_marker_after_failed_forced_refresh(tmp_path):
    clock = [1000]
    cache = DataCache(
        tmp_path, transport=lambda *a, **kw: Response({"timestamp": 1000}), clock=lambda: clock[0]
    )
    cache.get("world", WORLD_BASE + "pc", ttl=120)
    clock[0] += 20

    def offline(*a, **kw):
        raise requests.ConnectionError("offline")

    cache.transport = offline
    assert cache.get("world", WORLD_BASE + "pc", force=True)["stale"]
    assert cache.get("world", WORLD_BASE + "pc")["stale"]


def test_explicit_import_repairs_corrupted_notebook_and_preserves_original(tmp_path):
    broken = tmp_path / "warframe_state.json"
    broken.write_text("{broken", encoding="utf-8")
    store = WarframeStore(broken)
    assert store.error
    source = WarframeStore(tmp_path / "saved.json")
    source.add("goals", "Вернуть мои цели")
    store.import_file(source.path)
    assert not store.error and store.snapshot()["goals"][0]["name"] == "Вернуть мои цели"
    copies = list(tmp_path.glob("warframe_state.json.broken-*"))
    assert len(copies) == 1 and copies[0].read_text() == "{broken"


def test_repeated_price_notice_waits_full_interval_across_clock_boundaries(tmp_path):
    store = WarframeStore(tmp_path / "warframe_state.json")
    assert store.remember_notice("price:item", when=1799, repeat_after=1800)
    assert not store.remember_notice("price:item", when=1801, repeat_after=1800)
    restored = WarframeStore(store.path)
    assert not restored.remember_notice("price:item", when=3000, repeat_after=1800)
    assert restored.remember_notice("price:item", when=3599, repeat_after=1800)
