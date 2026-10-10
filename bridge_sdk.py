"""Optional line protocol for child scripts. Importing it starts nothing."""

import json
import os
from pathlib import Path
import sys

PREFIX = "KREST_EVENT "


def emit(kind, text="", **values):
    print(
        PREFIX + json.dumps({"type": kind, "text": str(text)[:2000], **values}, ensure_ascii=False),
        flush=True,
    )


def commands():
    for line in sys.stdin:
        if len(line) > 8192:
            continue
        try:
            data = json.loads(line)
            if isinstance(data, dict) and isinstance(data.get("command"), str):
                yield data
        except ValueError:
            continue


def data_dir():
    config_dir = (
        Path(os.environ["KREST_CONFIG_PATH"]).parent
        if os.getenv("KREST_CONFIG_PATH")
        else Path(__file__).resolve().parent
    )
    return Path(os.getenv("KREST_DATA_DIR", config_dir))
