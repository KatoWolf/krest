"""Support diagnostics omit secrets, personal note contents and command history."""

import platform
from datetime import datetime, timezone
from pathlib import Path
import importlib.metadata
import importlib.util


def build_report(runtime, config):
    modules = {}
    for name in ("psutil", "Pillow", "vosk", "torch", "winsdk", "pycaw", "pywinauto"):
        try:
            modules[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            modules[name] = "not installed"
    jobs = runtime.jobs.list(2000)
    cached = getattr(runtime, "cached", {})
    return {
        "version": 1,
        "created": datetime.now(timezone.utc).isoformat(),
        "platform": platform.system(),
        "release": platform.release(),
        "python": platform.python_version(),
        "dependencies": modules,
        "skills": len(runtime.manager.catalog()),
        "skill_error_modules": list(runtime.manager.errors)
        if isinstance(runtime.manager.errors, dict)
        else len(runtime.manager.errors),
        "jobs_by_status": {
            key: sum(row["status"] == key for row in jobs)
            for key in ("queued", "running", "completed", "failed", "interrupted", "cancelled")
        },
        "performance": runtime.performance.metrics,
        "safe_mode": cached.get("safe_mode", False),
        "schema_version": config.snapshot().get("schema_version"),
        "service_states": {
            name: {
                "running": row["running"],
                "health": row.get("health", "unknown"),
                "resources": row.get("resources", {}),
            }
            for name, row in cached.get("services", {}).items()
        },
    }


def export_report(runtime, config, destination):
    from storage import atomic_write_json

    atomic_write_json(Path(destination), build_report(runtime, config))
