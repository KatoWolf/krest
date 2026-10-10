"""Atomic JSON writes: readers never observe a half-written configuration."""

import json
import os
import tempfile
import time
from pathlib import Path


def atomic_write_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(40):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                # Windows readers/antivirus may hold a handle without FILE_SHARE_DELETE.
                # Keep the old complete file visible while waiting; never delete it first.
                if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 39:
                    raise
                time.sleep(min(0.002 * 1.5**attempt, 0.05))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path, encoding="utf-8-sig"):
    """Retry transient Windows sharing conflicts; malformed JSON still raises immediately."""
    for attempt in range(40):
        try:
            return json.loads(Path(path).read_text(encoding=encoding))
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == 39:
                raise
            time.sleep(min(0.002 * 1.5**attempt, 0.05))
