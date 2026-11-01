"""Selected-contact routing and durable at-most-once delivery for account bridges."""

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from storage import read_json

SERVICES = {"tg_bridge.py": "Telegram", "discord_account.py": "Discord"}
_cache = {}


class DeliveryAlreadyStarted(ValueError):
    """A reserved attempt without a final result cannot safely be repeated."""


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
                raise DeliveryAlreadyStarted(
                    "Эта отправка уже начиналась; автоматический повтор остановлен"
                ) from None
    finally:
        db.close()


def record_receipt(folder, service, request, receipt):
    """Commit the network result before emitting it over the fallible stdout pipe."""
    ident = str(request["request_id"])
    result = dict(
        id=ident,
        ok=receipt.get("ok") is True,
        uncertain=receipt.get("uncertain") is True,
        text=str(receipt.get("text", ""))[:250],
        message_id=str(receipt.get("message_id", ""))[:80],
    )
    db = sqlite3.connect(Path(folder) / "organizer_delivery.sqlite3", timeout=5)
    try:
        with db:
            if not db.execute(
                "SELECT 1 FROM delivered WHERE service=? AND id=?", (service, ident)
            ).fetchone():
                raise ValueError("Нет зарегистрированной отправки для этого результата")
            db.execute(
                "CREATE TABLE IF NOT EXISTS outcomes (service TEXT, id TEXT, chat TEXT, "
                "digest TEXT, result TEXT, created REAL, PRIMARY KEY(service,id))"
            )
            db.execute("DELETE FROM outcomes WHERE created<?", (time.time() - 7 * 86400,))
            # A duplicate failure must never overwrite a completed network result.
            db.execute(
                "INSERT OR IGNORE INTO outcomes VALUES(?,?,?,?,?,?)",
                (
                    service,
                    ident,
                    str(request["chat"]),
                    digest(request["text"]),
                    json.dumps(result, ensure_ascii=False),
                    time.time(),
                ),
            )
    finally:
        db.close()


def receipts(folder):
    """Read bounded delivery results, without creating or migrating an idle profile."""
    path = Path(folder) / "organizer_delivery.sqlite3"
    if not path.exists():
        return []
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='outcomes'"
        ).fetchone():
            return []  # Older releases only recorded that an attempt had begun.
        rows = []
        for source, ident, chat, checksum, encoded in db.execute(
            "SELECT service,id,chat,digest,result FROM outcomes "
            "WHERE created>=? ORDER BY created DESC LIMIT 1000",
            (time.time() - 7 * 86400,),
        ):
            try:
                result = json.loads(encoded)
            except (ValueError, TypeError):
                continue
            if (
                isinstance(result, dict)
                and result.get("id") == ident
                and type(result.get("ok")) is bool
            ):
                rows.append(
                    dict(service=source, id=ident, chat=chat, digest=checksum, result=result)
                )
        return rows
    finally:
        db.close()


def saved_receipt(folder, service, request):
    for row in receipts(folder):
        if (
            row["service"] == service
            and row["id"] == request.get("request_id")
            and row["chat"] == str(request.get("chat", ""))
            and row["digest"] == digest(request.get("text", ""))
        ):
            return row["result"]
    return None


def delivery_error(error):
    """Actionable account errors, without copying credentials or HTTP bodies to the UI."""
    name = type(error).__name__
    if isinstance(error, DeliveryAlreadyStarted):
        return (
            "Эта отправка уже начиналась, но её результат неизвестен. Проверьте чат; повтор остановлен.",
            True,
        )
    if name in {"PeerIdInvalid", "UserIdInvalid"}:
        return (
            "Telegram не знает этот контакт. Обновите список друзей и выберите его заново.",
            False,
        )
    if name in {"FloodWait", "RateLimited"}:
        return (
            "Мессенджер временно ограничил отправку сообщений. Дождитесь снятия ограничения.",
            False,
        )
    if name in {
        "UserIsBlocked",
        "YouBlockedUser",
        "UserPrivacyRestricted",
        "ChatWriteForbidden",
        "ChatForbidden",
    }:
        return (
            "Мессенджер запретил сообщение этому контакту. Проверьте блокировку и настройки приватности.",
            False,
        )
    if name in {
        "AuthKeyUnregistered",
        "SessionRevoked",
        "Unauthorized",
        "UserDeactivated",
        "UserDeactivatedBan",
    }:
        return "Сессия юзербота недействительна. Войдите заново в разделе подключений.", False
    if isinstance(error, PermissionError):
        return "Мессенджер отклонил доступ. Проверьте авторизацию юзербота и доступ к чату.", False
    if isinstance(error, ValueError) and not isinstance(error, json.JSONDecodeError):
        return str(error)[:250], False
    return (
        "Нет подтверждения от мессенджера. Проверьте соединение и чат; повторной отправки не было.",
        True,
    )
