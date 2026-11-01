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
    check_delivery_roundtrip()
    print(
        "Organizer: slang, voice parameters, reserve, disconnected bridge, delivery deduplication and restart passed; no live messages sent.",
        flush=True,
    )


def check_delivery_roundtrip():
    """Exercise the real account pipe and organizer; the network handler is local."""
    import asyncio
    import io
    import json
    import queue
    import tempfile
    import time
    from itertools import product
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock, patch
    import account_control
    from bridge_sdk import PREFIX
    from integrations import IntegrationManager
    from organizer_service import Organizer
    from organizer_bridge import claim, saved_receipt
    from smart_store import SmartStore

    for service, scenario in product(
        ("tg_bridge.py", "discord_account.py"),
        ("normal", "lost", "restart", "rejected", "timeout", "missing_id", "reserved"),
    ):
        with (
            tempfile.TemporaryDirectory(prefix="krest-delivery-") as folder,
            patch("licensing.require_premium"),
        ):
            path = Path(folder)
            cfg = {"profile": {"personality": "concise"}, "services": {}, "api_keys": {}}
            runtime = SimpleNamespace(
                settings=lambda: cfg,
                events=queue.Queue(),
                notifications=Mock(),
                center=Mock(),
                performance=SimpleNamespace(game=""),
            )
            runtime.center.muted.return_value = False
            manager = IntegrationManager(
                SimpleNamespace(config_mgr=SimpleNamespace(snapshot=lambda: cfg), runtime=runtime),
                base_dir=path,
            )
            runtime.integrations = manager
            org = Organizer(runtime, SmartStore(path / "store.sqlite3"), path)
            runtime.organizer = org
            pipe = io.StringIO()
            manager.processes[service] = SimpleNamespace(poll=lambda: None, stdin=pipe, pid=1)
            manager._event(
                service,
                PREFIX
                + json.dumps(
                    {"type": "ready", "capabilities": ["organizer_send", "organizer_contacts"]}
                ),
                cfg,
            )
            try:
                org.add_contact(name="Друг", service=service, chat="123456")
                org.call("preferences", messenger=service)
                org.create(size=2)
                org._send_one()
                request = json.loads(pipe.getvalue())
                sent, events = [], []
                if scenario == "reserved":
                    claim(path, service, request)  # Old/interrupted attempt without a receipt.

                async def send(chat, text):
                    sent.append((chat, text))
                    if scenario == "rejected":
                        raise type("PeerIdInvalid", (Exception,), {})()
                    if scenario == "timeout":
                        raise TimeoutError("Connection lost")
                    if scenario == "missing_id":
                        return None
                    return "987654"

                def receive(kind, **values):
                    events.append((kind, values))
                    if scenario not in {"lost", "restart"}:
                        manager._event(service, PREFIX + json.dumps({"type": kind, **values}), cfg)

                async def exercise():
                    control = account_control.AccountControl.__new__(account_control.AccountControl)
                    control.seen, control.service, control.folder = set(), service, path
                    control.attach(send, send)
                    try:
                        await asyncio.to_thread(control._read)
                    finally:
                        control.detach()

                # Duplicate commands may replay a result, but never the network operation.
                with (
                    patch.object(account_control, "commands", lambda: iter([request, request])),
                    patch.object(
                        account_control,
                        "emit",
                        receive,
                    ),
                ):
                    asyncio.run(exercise())
                if scenario == "restart":
                    org.close()
                    org = Organizer(runtime, org.store, path)
                    runtime.organizer = org
                    org.call("delivery_check")
                    if org.state["party"]["status"] != "paused":
                        raise RuntimeError("Сверка доставки возобновила сбор после перезапуска")
                while not org.inbox.empty():
                    kind, source, data = org.inbox.get_nowait()
                    if kind == "receipt":
                        org.receipt(source, data)
                org._tick(time.time() + 36)
                row = org.state["outbox"][0]
                expected = (
                    "failed"
                    if scenario == "rejected"
                    else "uncertain"
                    if scenario in {"timeout", "missing_id", "reserved"}
                    else "delivered"
                )
                if row["status"] != expected:
                    raise RuntimeError(
                        f"Неверный результат доставки: {service}, {scenario}, {row['status']}"
                    )
                if len(sent) != (0 if scenario == "reserved" else 1):
                    raise RuntimeError("Повтор поручения повторно отправил сообщение")
                if expected == "delivered":
                    if (
                        row.get("message_id") != "987654"
                        or org.state["party"]["members"][0]["state"] != "invited"
                    ):
                        raise RuntimeError("Подтверждение доставки не обновило участника")
                    if any(not values["data"]["action"]["ok"] for _, values in events):
                        raise RuntimeError("Повтор поручения изменил результат")
                if scenario == "rejected" and "контакт" not in row.get("detail", ""):
                    raise RuntimeError("Потеряна причина отказа Telegram")
                for change in (
                    {"chat": "999999"},
                    {"text": "Другое сообщение"},
                    {"request_id": "wrong"},
                ):
                    if saved_receipt(path, service, {**request, **change}) is not None:
                        raise RuntimeError("Подтверждение применилось к другому поручению")
                other = "discord_account.py" if service == "tg_bridge.py" else "tg_bridge.py"
                if saved_receipt(path, other, request) is not None:
                    raise RuntimeError("Подтверждение применилось к другому мессенджеру")
                before = pipe.getvalue()
                org.call("delivery_check")
                if pipe.getvalue() != before:
                    raise RuntimeError("Проверка доставки отправила новое сообщение")
            finally:
                org.close()
