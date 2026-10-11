"""SDK transport observations are local, and never substitute for API confirmation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tg_bridge import TelegramHealth, session_transport_state, status_updater


@pytest.fixture
def events(monkeypatch):
    import bridge_sdk

    result = []
    monkeypatch.setattr(bridge_sdk, "emit", lambda kind, text, **kw: result.append((kind, kw)))
    return result


def test_sdk_clear_set_requires_existing_api_success(events):
    event = asyncio.Event()
    event.set()
    health = TelegramHealth(SimpleNamespace(session=SimpleNamespace(is_started=event)))
    health.sample()
    assert not events
    event.clear()
    health.sample()
    health.sample()
    assert events == [("status", {"health": "reconnecting"})]
    event.set()
    health.sample()
    health.sample()
    assert events[-1] == ("status", {"health": "unknown"})
    assert not any(kind == "ready" for kind, _ in events)
    health.confirmed()
    assert events[-1] == ("ready", {})
    assert health.state == "ready"


def test_unobservable_or_lifecycle_bool_is_unknown(events):
    client = SimpleNamespace(is_connected=True, session=SimpleNamespace(is_connected=True))
    assert session_transport_state(client) is None
    health = TelegramHealth(client)
    health.sample()
    health.confirmed()
    assert events == [("status", {"health": "unknown"})]


def test_compatible_sdk_connected_event(events):
    event = asyncio.Event()
    client = SimpleNamespace(session=SimpleNamespace(is_connected=event))
    assert session_transport_state(client) is False
    event.set()
    assert session_transport_state(client) is True


def test_monitor_cancels_without_network_or_extra_tasks(events):
    client = SimpleNamespace(session=SimpleNamespace(is_started=asyncio.Event()))

    async def exercise():
        health = TelegramHealth(client)
        task = asyncio.create_task(health.watch())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()

    asyncio.run(exercise())
    assert events == [("status", {"health": "reconnecting"})]


@pytest.mark.parametrize("failure", [False, True])
def test_existing_profile_call_reports_success_or_unknown_without_extra_requests(
    events, monkeypatch, failure
):
    import tg_bridge

    event = asyncio.Event()
    event.set()
    update = AsyncMock(side_effect=OSError("token-sensitive") if failure else None)
    client = SimpleNamespace(session=SimpleNamespace(is_started=event), update_profile=update)
    health = TelegramHealth(client)
    health.state = "unknown"
    sleeps = []

    async def stop_after_one(seconds):
        sleeps.append(seconds)
        raise asyncio.CancelledError()

    monkeypatch.setattr(tg_bridge.asyncio, "sleep", stop_after_one)
    monkeypatch.setattr(tg_bridge, "get_local_status", AsyncMock(return_value={}))

    async def exercise():
        with pytest.raises(asyncio.CancelledError):
            await status_updater(client, health)

    asyncio.run(exercise())
    update.assert_awaited_once()
    assert sleeps == [300]
    assert health.state == ("unknown" if failure else "ready")
    assert not failure or not any(kind == "ready" for kind, _ in events)
