"""Offline authentication lifecycle checks; no Telegram requests are made."""

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def auth(tmp_path, monkeypatch):
    import tg_bridge

    monkeypatch.setattr(tg_bridge, "BASE_DIR", str(tmp_path))
    client = SimpleNamespace(
        is_connected=True,
        connect=AsyncMock(),
        disconnect=AsyncMock(),
        get_me=AsyncMock(return_value=None),
        send_code=AsyncMock(return_value=SimpleNamespace(phone_code_hash="private-code-hash")),
        sign_in=AsyncMock(),
    )
    monkeypatch.setattr(tg_bridge, "Client", lambda *args, **kwargs: client)
    return tg_bridge, tg_bridge.AuthManager(), client


def test_auth_marker_has_no_secrets_and_login_uses_memory(auth):
    module, manager, client = auth

    async def exercise():
        await manager._async_send_code("12345", "private-api-hash", "+79991234567")
        text = Path(manager.state_file).read_text()
        for secret in ("private-api-hash", "private-code-hash", "+79991234567"):
            assert secret not in text
        assert "api_id" not in json.loads(text)
        assert module.auth_state_active(manager.state_file)
        client.get_me.return_value = SimpleNamespace(
            first_name="Owner", last_name="", username=None
        )
        assert await manager._async_login("123456") == "Owner"
        client.sign_in.assert_awaited_once_with("+79991234567", "private-code-hash", "123456")
        assert not Path(manager.state_file).exists()
        assert manager.client is None and manager._pending_auth is None
        assert manager.phone is None and manager.phone_code_hash is None
        assert manager._session_lock is None

    asyncio.run(exercise())


@pytest.mark.parametrize("stage", ["connect", "send_code", "sign_in"])
def test_cancel_auth_releases_client_marker_and_lock(auth, stage):
    module, manager, client = auth

    async def exercise():
        entered = asyncio.Event()

        async def blocked(*args):
            entered.set()
            await asyncio.Event().wait()

        if stage == "sign_in":
            await manager._async_send_code("12345", "private-api-hash", "+79991234567")
            client.sign_in = blocked
            coroutine = manager._async_login("123456")
        else:
            setattr(client, stage, blocked)
            if stage == "connect":
                client.is_connected = False
                client.session = SimpleNamespace(stop=AsyncMock())
                client.storage = SimpleNamespace(close=AsyncMock())
            coroutine = manager._async_send_code("12345", "private-api-hash", "+79991234567")
        task = asyncio.create_task(coroutine)
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not Path(manager.state_file).exists()
        assert manager.client is None and manager._pending_auth is None
        assert manager._session_lock is None
        if stage == "connect":
            client.disconnect.assert_not_awaited()
            client.session.stop.assert_awaited_once()
            client.storage.close.assert_awaited_once()
        else:
            client.disconnect.assert_awaited()
        assert not module.auth_state_active(manager.state_file)

    asyncio.run(exercise())


@pytest.mark.parametrize("has_session", [False, True])
def test_partial_connect_closes_storage_even_if_session_stop_fails(auth, has_session):
    _, manager, client = auth
    client.is_connected = False
    client.storage = SimpleNamespace(close=AsyncMock())
    client.session = (
        SimpleNamespace(stop=AsyncMock(side_effect=ConnectionError("incomplete socket")))
        if has_session
        else None
    )

    async def exercise():
        manager.client = client
        await manager._disconnect_client()
        client.disconnect.assert_not_awaited()
        client.storage.close.assert_awaited_once()
        assert manager.client is None

    asyncio.run(exercise())


def test_cancel_real_pyrogram_connect_before_connected_flag(auth, monkeypatch):
    _, manager, client = auth
    from pyrogram.methods.auth.connect import Connect
    import importlib

    connect_module = importlib.import_module("pyrogram.methods.auth.connect")

    async def exercise():
        started = asyncio.Event()

        async def start():
            started.set()
            await asyncio.Event().wait()

        session = SimpleNamespace(start=start, stop=AsyncMock())
        client.is_connected = False
        client.load_session = AsyncMock()
        client.storage = SimpleNamespace(
            dc_id=AsyncMock(return_value=2),
            auth_key=AsyncMock(return_value=b"fake"),
            test_mode=AsyncMock(return_value=False),
            close=AsyncMock(),
        )
        monkeypatch.setattr(connect_module, "Session", lambda *args: session)
        client.connect = lambda: Connect.connect(client)
        task = asyncio.create_task(manager._async_send_code("12345", "secret", "+79991234567"))
        await started.wait()
        assert not client.is_connected and client.session is session
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        session.stop.assert_awaited_once()
        client.storage.close.assert_awaited_once()
        client.disconnect.assert_not_awaited()
        assert manager.client is None and manager._session_lock is None
        assert not Path(manager.state_file).exists()

    asyncio.run(exercise())


def test_foreign_auth_cannot_replace_active_attempt(auth):
    module, manager, client = auth

    async def exercise():
        await manager._async_send_code("12345", "secret", "+79991234567")
        original = Path(manager.state_file).read_text()
        other = module.AuthManager()
        with pytest.raises(RuntimeError, match="другом окне"):
            await other._async_send_code("12345", "other-secret", "+79991234567")
        with pytest.raises(ValueError, match="перезапуска"):
            await other._async_login("123456")
        assert Path(manager.state_file).read_text() == original
        client.disconnect.assert_not_awaited()
        await manager._clear_auth()

    asyncio.run(exercise())


def test_legacy_auth_secrets_are_not_reused_and_new_attempt_replaces_them(auth):
    module, manager, client = auth
    Path(manager.state_file).write_text(
        json.dumps(
            {
                "status": "code_sent",
                "api_hash": "legacy-secret",
                "phone_code_hash": "old-code",
                "phone": "+79991234567",
                "created_at": time.time(),
            }
        )
    )
    assert not module.auth_state_active(manager.state_file)

    async def exercise():
        with pytest.raises(ValueError, match="перезапуска"):
            await manager._async_login("123456")
        client.sign_in.assert_not_awaited()
        await manager._async_send_code("12345", "new-secret", "+79991234567")
        assert "legacy-secret" not in Path(manager.state_file).read_text()
        await manager._clear_auth()

    asyncio.run(exercise())


def test_bridge_rejects_dead_expired_and_oversized_auth_markers(auth, monkeypatch):
    module, manager, _ = auth
    import psutil

    payload = {
        "status": "code_sent",
        "attempt": "test",
        "pid": os.getpid(),
        "process_started": psutil.Process().create_time(),
        "created_at": time.time(),
    }
    path = Path(manager.state_file)
    path.write_text(json.dumps(payload))
    assert module.auth_state_active(path)
    payload["created_at"] -= module.AUTH_STATE_TTL + 1
    path.write_text(json.dumps(payload))
    assert not module.auth_state_active(path)
    payload["created_at"] = time.time()
    payload["process_started"] -= 10
    path.write_text(json.dumps(payload))
    assert not module.auth_state_active(path)
    payload["pid"] = 999999999
    path.write_text(json.dumps(payload))
    assert not module.auth_state_active(path)
    path.write_text(" " * 5000)
    assert not module.auth_state_active(path)


def test_public_timeout_waits_for_coroutine_cleanup(auth):
    _, manager, client = auth
    disconnected = threading.Event()

    async def blocked(*args):
        await asyncio.Event().wait()

    async def disconnect():
        await asyncio.sleep(0.05)
        disconnected.set()

    client.send_code = blocked
    client.disconnect = disconnect
    try:
        with pytest.raises(TimeoutError, match="test timeout"):
            manager._run_operation(
                lambda: manager._async_send_code("12345", "secret", "+79991234567"),
                0.03,
                "test timeout",
            )
        assert disconnected.is_set()
        assert manager._operation_done.is_set()
        assert manager.client is None and not Path(manager.state_file).exists()
    finally:
        manager.loop.call_soon_threadsafe(manager.loop.stop)
        manager.thread.join(2)


def test_auth_expiry_releases_waiting_handshake(auth, monkeypatch):
    module, manager, _ = auth

    async def exercise():
        monkeypatch.setattr(module, "AUTH_STATE_TTL", 0.02)
        await manager._async_send_code("12345", "secret", "+79991234567")
        await asyncio.sleep(0.08)
        assert manager.client is None and manager._session_lock is None
        assert manager._pending_auth is None and not Path(manager.state_file).exists()

    asyncio.run(exercise())


def test_expiry_cleanup_cannot_clear_a_new_auth_attempt(auth, monkeypatch):
    module, manager, client = auth

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def disconnect():
            entered.set()
            await release.wait()

        client.disconnect = disconnect
        monkeypatch.setattr(module, "AUTH_STATE_TTL", 0.02)
        await manager._async_send_code("12345", "first-secret", "+79991234567")
        await entered.wait()
        monkeypatch.setattr(module, "AUTH_STATE_TTL", 600)
        pending = asyncio.create_task(
            manager._async_send_code("12345", "second-secret", "+79991234567")
        )
        await asyncio.sleep(0)
        assert not pending.done()
        release.set()
        await pending
        assert manager.client is client
        assert manager._pending_auth["api_hash"] == "second-secret"
        assert module.auth_state_active(manager.state_file)
        await manager._clear_auth()

    asyncio.run(exercise())
