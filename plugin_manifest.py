"""Optional manifest compatibility and bridge permissions, not an OS sandbox."""

from pathlib import Path
import hashlib
import json

PERMISSIONS = {"notifications", "refresh"}


def inspect_plugin(script):
    script = Path(script).resolve()
    manifest = script.with_suffix(".krest.json")
    data = {
        "api_version": 1,
        "name": script.stem,
        "permissions": ["notifications", "refresh"],
        "manifest": False,
    }
    if manifest.exists():
        if manifest.stat().st_size > 32000:
            raise ValueError("Манифест слишком большой")
        data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("api_version") != 1:
            raise ValueError("Плагин требует несовместимую версию протокола")
        if not isinstance(data.get("name"), str) or not 1 <= len(data["name"]) <= 80:
            raise ValueError("В манифесте нужно название до 80 символов")
        permissions = data.get("permissions", [])
        if not isinstance(permissions, list) or any(p not in PERMISSIONS for p in permissions):
            raise ValueError("Неизвестное разрешение моста")
        data["manifest"] = True
    data["sha256"] = hashlib.sha256(script.read_bytes()).hexdigest()
    return data
