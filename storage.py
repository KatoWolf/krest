"""Atomic JSON writes: readers never observe a half-written configuration."""

import json
import errno
import os
import tempfile
import time
from pathlib import Path


def _sharing_conflict(error):
    # CRT-backed open() often exposes only errno=EACCES, without a winerror field.
    return getattr(error, "winerror", None) in (5, 32, 33) or (
        os.name == "nt" and error.errno in (errno.EACCES, errno.EPERM)
    )


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
                if not _sharing_conflict(exc) or attempt == 39:
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
            if not _sharing_conflict(exc) or attempt == 39:
                raise
            time.sleep(min(0.002 * 1.5**attempt, 0.05))
