"""Portable release checks for file commits, resume and the light background budget."""

import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


def require(value, message):
    if not value:
        raise RuntimeError(message)


def check_background():
    from autonomy_files import CHUNK, FileJobs, fingerprint
    from autonomy_store import DutyStore
    from file_moves import MoveJournal
    from incoming_files import LightBudget
    from reconnect_budget import ReconnectBudget

    with tempfile.TemporaryDirectory(prefix="krest-files-") as folder:
        root = Path(folder)
        inbox, output = root / "Downloads", root / "Documents"
        inbox.mkdir()
        output.mkdir()

        def old(path, data=b"original"):
            path.write_bytes(data)
            os.utime(path, (time.time() - 300, time.time() - 300))
            return path

        source = old(inbox / "report.txt")
        old(output / source.name, b"existing")
        journal = MoveJournal(inbox)
        inode = source.stat().st_ino
        row = journal.move(source, output)
        target = Path(row["target"])
        require(target.stat().st_ino == inode and not source.exists(), "Light move copied the file")
        require(
            (output / source.name).read_bytes() == b"existing", "Move overwrote an occupied name"
        )
        journal.undo(row["id"])
        require(journal.was_returned(source, fingerprint(source)), "Undo would be sorted again")
        row = journal.move(source, output)
        target = Path(row["target"])
        stamp = target.stat()
        time.sleep(0.02)
        target.write_bytes(b"modified")
        os.utime(target, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
        try:
            journal.undo(row["id"])
        except ValueError:
            pass
        else:
            raise RuntimeError("Undo accepted a modified file with preserved mtime/size")

        source = old(inbox / "locked.txt")
        if os.name == "nt":
            with source.open("rb"):
                try:
                    journal.move(source, output)
                except OSError:
                    pass
                else:
                    raise RuntimeError("Open Windows file was moved")
            require(source.exists(), "Open file disappeared")

        class InterruptedJournal(MoveJournal):
            def _update(self, ident, status, detail=""):
                if status == "moved":
                    raise RuntimeError("simulated interruption after file commit")
                return super()._update(ident, status, detail)

        source = old(inbox / "interrupted.txt")
        try:
            InterruptedJournal(inbox).move(source, output)
        except RuntimeError:
            pass
        journal.recover()
        moved = next(r for r in journal.rows() if r["source"] == str(source))
        require(moved["status"] == "moved", "Committed move was not recovered")
        journal.recover()
        require(
            Path(moved["target"]).read_bytes() == b"original", "Recovery changed a committed file"
        )

        store = DutyStore(root / "duty.sqlite3")
        files = FileJobs(store, lambda: None, lambda _: None)
        source = old(inbox / "partial.bin", b"a" * CHUNK + b"b" * CHUNK)
        target = output / source.name
        ident = store.enqueue("copy", "Interrupted copy", {})
        job = store.get(ident)
        job["checkpoint"] = {
            "copies": {str(target): {"stamp": fingerprint(source), "bytes": CHUNK}}
        }
        partial = target.with_name("." + target.name + "." + ident + ".partial")
        partial.write_bytes(b"x" * CHUNK)
        store.update(ident, status="running", checkpoint=job["checkpoint"])
        recovered = DutyStore(store.path).get(ident)
        require(
            recovered["recovered"] and recovered["status"] == "waiting",
            "Copy was not marked interrupted",
        )
        digest = files.copy_file(source, target, recovered)
        require(
            target.read_bytes() == source.read_bytes() and not partial.exists(),
            "Damaged partial was reused",
        )

        # Exercise the verified-copy commit used for transfers between volumes.
        try:
            InterruptedJournal(inbox).finish_copy(source, target, fingerprint(source), digest)
        except RuntimeError:
            pass
        journal.recover_copies()
        moved = next(r for r in journal.rows() if r["source"] == str(source))
        require(
            moved["status"] == "moved" and not source.exists(),
            "Verified transfer was not recovered",
        )
        ident = store.enqueue("file_return", "Return copy", {})
        journal.undo_copy(moved["id"], files, store.get(ident), lambda: None)
        require(source.is_file() and not target.exists(), "Verified return did not finish")
        require(
            journal.was_returned(source, fingerprint(source)), "Verified return was not recorded"
        )

        install = store.enqueue("software", "Interrupted install", {})
        store.update(install, status="running", checkpoint={"installer_started": True})
        require(
            DutyStore(store.path).get(install)["status"] == "failed", "Installer would be replayed"
        )

    cfg = {"file_background": {"paused": False}}
    rt = SimpleNamespace(
        settings=lambda: cfg,
        stop_event=threading.Event(),
        performance=SimpleNamespace(game="dota2.exe"),
    )
    budget = LightBudget(rt)
    with (
        patch("incoming_files.psutil.cpu_percent", return_value=10),
        patch(
            "incoming_files.psutil.virtual_memory",
            return_value=SimpleNamespace(available=2 * 1024**3),
        ),
        patch("incoming_files.psutil.disk_io_counters", return_value=None),
    ):
        require(budget.allowed()[0], "Game alone blocked light work")
        budget.voice()
        require(not budget.allowed()[0], "Voice did not preempt light work")
    now = [1000.0]
    reconnect = ReconnectBudget(lambda: now[0])
    for delay in (0, 3, 10):
        now[0] += delay
        require(reconnect.allow(), "Reconnect budget rejected an eligible attempt")
    now[0] += 60
    require(not reconnect.allow(), "Reconnect loop exceeded its attempt budget")
    reconnect.reset()
    require(reconnect.allow(), "Hardware refresh did not rearm microphone recovery")
    print(
        "Background files: exclusive moves, open-file guard, verified resume/return and bounded recovery passed.",
        flush=True,
    )


if __name__ == "__main__":
    check_background()
