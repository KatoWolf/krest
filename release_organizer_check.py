"""Portable coordinator checks, using an in-memory transport and no real accounts."""


def check_organizer():
    import queue
    import tempfile
    import time
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    from organizer_service import Organizer
    from organizer_language import interpret, PHRASES
    from organizer_commands import parse_create
    from organizer_bridge import claim
    from organizer_messages import BANK, FEATURES, preview
    from personality import PERSONALITIES
    from smart_store import SmartStore

    if sum(map(len, PHRASES.values())) < 600:
        raise RuntimeError("Словарь ответов неполон")
    if BANK.keys() != PERSONALITIES.keys() or BANK.keys() != FEATURES.keys():
        raise RuntimeError("Не для всех личностей есть реплики организатора")
    for style, events in BANK.items():
        if any(len(set(variants)) != 4 for variants in events.values()):
            raise RuntimeError("Неполный набор вариантов личности: " + style)
        for variant in range(4):
            data = preview({"profile": {"personality": style}}, variant)
            if data["style"] != style or "Dota 2" not in data["invitation"]:
                raise RuntimeError("Неверное превью личности: " + style)
    cases = {
        "го": "ready",
        "мб позже": "uncertain",
        "мин через 20 залечу": "delayed",
        "не готов": "waiting",
        "готов?": "question",
        "он говорит я готов": "unknown",
        "не смогу": "declined",
        "yt ujnjd": "waiting",
    }
    for phrase, kind in cases.items():
        if interpret(phrase, time.time()).kind != kind:
            raise RuntimeError("Неверный разбор ответа: " + phrase)
    if parse_create("собери нас вечером в доту начинаем когда будет пятеро") != dict(
        game="доту", when="вечером", size=5
    ):
        raise RuntimeError("Не разобраны параметры голосового сбора")
    with (
        tempfile.TemporaryDirectory(prefix="krest-organizer-check-") as folder,
        patch("licensing.require_premium"),
    ):
        path = Path(folder)
        runtime = SimpleNamespace(
            settings=lambda: {
                "owner_name": "Тест",
                "profile": {"personality": "concise"},
                "api_keys": {"steam_id": "", "steam_api_key": ""},
            },
            events=queue.Queue(),
            notifications=Mock(),
            center=Mock(),
            performance=SimpleNamespace(game=""),
            integrations=Mock(),
        )
        runtime.center.muted.return_value = False
        runtime.integrations.details.return_value = {
            "bridge": {"ready": True, "capabilities": ["organizer_send"]}
        }
        org = Organizer(runtime, SmartStore(path / "store.sqlite3"), path)
        try:
            for i in range(3):
                org.add_contact(name="Друг " + str(i), service="tg_bridge.py", chat=str(10000 + i))
            org.create(size=3)
            if len(org.state["outbox"]) != 2:
                raise RuntimeError("Нарушено число приглашений")
            row = org.state["outbox"][0]
            payload = {"request_id": row["id"], "chat": row["chat"], "text": row["text"]}
            claim(path, "tg_bridge.py", payload)
            try:
                claim(path, "tg_bridge.py", payload)
            except ValueError:
                pass
            else:
                raise RuntimeError("Повторная отправка не остановлена")
            org._send_one()
            if runtime.integrations.send.call_count != 1:
                raise RuntimeError("Команда не передана в тестовый мост")
            org.last_send = 0
            org.runtime.integrations.details.return_value = {"bridge": {"ready": False}}
            org._send_one()
            if runtime.integrations.send.call_count != 1:
                raise RuntimeError("Отключённый мост получил отправку")
            org.receive("tg_bridge.py", {"chat": "10000", "message": "1", "text": "пас"})
            if org.state["party"]["members"][2]["state"] != "queued":
                raise RuntimeError("Резерв не приглашён после отказа")
            org.receive("tg_bridge.py", {"chat": "10001", "message": "2", "text": "через 20 минут"})
            org._tick(time.time() + 1201)
            if org.state["party"]["status"] == "ready":
                raise RuntimeError("Время ожидания ошибочно принято за готовность")
            restored = Organizer(runtime, org.store, path)
            try:
                if restored.state["party"]["status"] != "paused":
                    raise RuntimeError("Сбор сам возобновился после перезапуска")
            finally:
                restored.close()
        finally:
            org.close()
    print(
        "Organizer: slang, voice parameters, reserve, disconnected bridge, delivery deduplication and restart passed; no live messages sent.",
        flush=True,
    )
