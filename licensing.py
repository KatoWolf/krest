"""Offline, signed Premium codes. The application contains only a public key."""

import base64
from functools import lru_cache
import json
from pathlib import Path
import time

PUBLIC_KEY_PATH = Path(__file__).with_name("license_public_key.pem")


def _decode(text):
    return base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)


@lru_cache(maxsize=32)
def _verify(code, public_key):
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    prefix, body, signature = code.split(".")
    if prefix != "KREST1":
        raise ValueError("format")
    key = load_pem_public_key(public_key)
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("key type")
    raw = _decode(body)
    key.verify(_decode(signature), raw)
    value = json.loads(raw)
    if (
        not isinstance(value, dict)
        or value.get("product") != "krest"
        or value.get("plan") != "premium"
    ):
        raise ValueError("product")
    if not isinstance(value.get("id"), str) or not value["id"]:
        raise ValueError("id")
    for field in ("issued", "expires"):
        if value.get(field) is not None and (type(value[field]) is not int or value[field] < 0):
            raise ValueError("time")
    if type(value.get("issued")) is not int:
        raise ValueError("issued")
    return value


def status(settings, now=None):
    code = settings.get("license", {}).get("code", "").strip()
    base = {"premium": False, "message": "Обычная версия", "expires": None, "id": ""}
    if not code:
        return base
    if len(code) > 4096:
        return {**base, "message": "Код слишком длинный"}
    try:
        value = _verify(code, PUBLIC_KEY_PATH.read_bytes())
    except FileNotFoundError:
        return {**base, "message": "Отсутствует открытый ключ издателя"}
    except Exception:
        return {**base, "message": "Код не прошёл проверку подписи"}
    now = time.time() if now is None else now
    if value["issued"] > now + 300:
        return {**base, "message": "Дата выдачи кода ещё не наступила. Проверьте часы компьютера"}
    if value.get("expires") is not None and now >= value["expires"]:
        return {**base, "message": "Срок Premium истёк", "expires": value["expires"]}
    return {
        "premium": True,
        "message": "Premium активен",
        "expires": value.get("expires"),
        "id": value["id"],
    }


def require_premium(settings):
    if not status(settings)["premium"]:
        raise PermissionError(
            "Подключение ботов доступно в Premium. Активируйте код во вкладке «Premium»."
        )
