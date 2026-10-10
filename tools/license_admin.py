"""Issuer-only utility: keep the private key outside the distributed application."""

import argparse
import base64
import json
from pathlib import Path
import os
import time
import uuid
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def generate(private_path, public_path):
    key = Ed25519PrivateKey.generate()
    private_path, public_path = Path(private_path), Path(public_path)
    if private_path.exists() or public_path.exists():
        raise ValueError("Ключи уже существуют; автоматическая перезапись запрещена")
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fd = os.open(private_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(private)
    with public_path.open("xb") as stream:
        stream.write(public)


def issue(private_path, days=0):
    if days < 0:
        raise ValueError("Срок должен быть неотрицательным")
    key = serialization.load_pem_private_key(Path(private_path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("Нужен закрытый ключ Ed25519")
    now = int(time.time())
    value = {
        "product": "krest",
        "plan": "premium",
        "id": str(uuid.uuid4()),
        "issued": now,
        "expires": now + days * 86400 if days else None,
    }
    raw = json.dumps(value, separators=(",", ":")).encode()
    encode = lambda data: base64.urlsafe_b64encode(data).decode().rstrip("=")
    return "KREST1." + encode(raw) + "." + encode(key.sign(raw))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "issue"])
    parser.add_argument("--private", required=True, help="Закрытый ключ вне папки приложения")
    parser.add_argument("--public", default="license_public_key.pem")
    parser.add_argument("--days", type=int, default=0, help="0 — бессрочно")
    parser.add_argument("--output", help="Файл кода; если не задан, код выводится в терминал")
    args = parser.parse_args()
    if args.action == "init":
        generate(args.private, args.public)
        print(
            "Ключи созданы. Храните закрытый ключ отдельно; передавайте приложению только открытый."
        )
    else:
        code = issue(args.private, args.days)
        if args.output:
            with Path(args.output).open("x", encoding="utf-8") as stream:
                stream.write(code + "\n")
        else:
            print(code)


if __name__ == "__main__":
    main()
