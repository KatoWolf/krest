"""Selected-contact routing and durable at-most-once delivery for account bridges."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from storage import read_json

SERVICES = {"tg_bridge.py": "Telegram", "discord_account.py": "Discord"}
_cache = {}


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def routes(folder):
    path = Path(folder) / "organizer_routes.json"
    now = time.monotonic()
    cached = _cache.get(str(path))
    if cached and now - cached[0] < 1:
        return cached[1]
    try:
        data = read_json(path) if path.stat().st_size < 150_000 else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    _cache[str(path)] = (now, data)
    if len(_cache) > 8:
        _cache.clear()
    return data


def inbound(folder, service, chat, message, text, *, deferred=False):
    from bot_activity import local_activity
    from organizer_language import request_text
    from bridge_sdk import emit

    if not str(chat).isdigit() or not str(message) or not isinstance(text, str) or not text.strip():
        return False
    data = routes(folder)
    contact = next(
        (r for r in data.get("contacts", []) if r["service"] == service and r["chat"] == str(chat)),
        None,
    )
    if not contact or not local_activity(folder):
        return False
    if not contact.get("party") and not (contact.get("requests") and request_text(text)):
        return False
    if deferred:
        return True  # Suppress an older pending auto-reply, never replay it as a new answer.
    emit("organizer_incoming", text[:2000], chat=str(chat), message=str(message)[:80])
    return True


def claim(folder, service, request):
    """Reserve before the network call. An interrupted send is never replayed."""
    now = time.time()
    ident, chat, text = request.get("request_id"), str(request.get("chat", "")), request.get("text")
    if (
        service not in SERVICES
        or not isinstance(ident, str)
        or not 1 <= len(ident) <= 80
        or not isinstance(text, str)
    ):
        raise ValueError("Некорректное поручение организатора")
    # Do not use the cached route here: cancellation must revoke queued sends.
    data = read_json(Path(folder) / "organizer_routes.json")
    allowed = next((r for r in data.get("messages", []) if r.get("id") == ident), None)
    if (
        not allowed
        or allowed["service"] != service
        or allowed["chat"] != chat
        or allowed["digest"] != digest(text)
        or allowed["expires"] <= now
    ):
        raise ValueError("Поручение отменено, изменилось или истекло")
    db = sqlite3.connect(Path(folder) / "organizer_delivery.sqlite3", timeout=5)
    try:
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS delivered(service TEXT, id TEXT, created REAL, PRIMARY KEY(service,id))"
            )
            db.execute("DELETE FROM delivered WHERE created<?", (now - 7 * 86400,))
            try:
                db.execute("INSERT INTO delivered VALUES(?,?,?)", (service, ident, now))
            except sqlite3.IntegrityError:
                raise ValueError(
                    "Эта отправка уже начиналась; автоматический повтор остановлен"
                ) from None
    finally:
        db.close()
