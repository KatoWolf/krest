"""Expose failing test names and concise diagnostics in ordinary CI annotations."""

import sys
from pathlib import Path
import xml.etree.ElementTree as ET


def report(path):
    if not Path(path).is_file():
        return
    root = ET.parse(path).getroot()
    for case in root.iter("testcase"):
        for tag in ("failure", "error"):
            result = case.find(tag)
            if result is not None:
                text = (
                    case.get("classname", "")
                    + "."
                    + case.get("name", "")
                    + ": "
                    + (result.text or result.get("message", ""))[-6000:]
                )
                print(
                    "::error::" + text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
                )


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    report(sys.argv[1])
