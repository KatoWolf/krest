"""Exercise an isolated kernel without voice models, a display or external accounts.

python tools/soak_kernel.py --seconds 600 --output /tmp/krest-soak.json
"""

import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psutil
from configuration import ConfigManager
from kernel_client import KernelClient, RemoteRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 86400:
        parser.error("Use 5–86400 seconds")
    with tempfile.TemporaryDirectory(prefix="krest-soak-") as folder:
        cfg = ConfigManager(Path(folder) / "config.json")
        data = cfg.snapshot()
        data["owner_name"] = "Тест нагрузки"
        data["profile"]["onboarding_completed"] = True
        cfg.save_config(data)
        client = KernelClient(cfg.filepath).ensure_started()
        process = psutil.Process(client.call("ping")["pid"])
        latencies, samples = [], []
        start = time.monotonic()
        accepted, reconnects, rejected = 0, 0, 0
        next_reconnect = start
        try:
            for i in range(40):
                ok = bool(client.call("submit", text="время", request_key="burst-" + str(i)))
                accepted += ok
                rejected += not ok
            timer = client.call("state.add_timer", args=[2, "Проверка независимого таймера"])
            process.cpu_percent(None)
            while time.monotonic() - start < args.seconds:
                before = time.monotonic()
                client.call("snapshot")
                latencies.append((time.monotonic() - before) * 1000)
                samples.append(
                    {
                        "rss_mb": round(process.memory_info().rss / 1024**2, 2),
                        "cpu": process.cpu_percent(None),
                        "threads": process.num_threads(),
                        "fds": process.num_fds()
                        if hasattr(process, "num_fds")
                        else process.num_handles(),
                    }
                )
                if time.monotonic() >= next_reconnect:
                    remote = RemoteRuntime(client, cfg.snapshot)
                    remote.detach()
                    ok = bool(
                        client.call(
                            "submit", text="план на сегодня", request_key="cycle-" + str(reconnects)
                        )
                    )
                    accepted += ok
                    rejected += not ok
                    reconnects += 1
                    next_reconnect = time.monotonic() + 30
                time.sleep(2)
            deadline = time.monotonic() + 10
            while True:
                jobs = client.call("jobs")
                if all(row["status"] == "completed" for row in jobs):
                    break
                if time.monotonic() >= deadline:
                    raise RuntimeError("Some accepted commands did not complete")
                time.sleep(0.1)
            # Job history is deliberately bounded to 2000 records.
            assert len(jobs) == min(accepted, 2000), (len(jobs), accepted)
            assert any(row["id"] == timer["id"] for row in client.call("snapshot")["reminders"])
            report = {
                "duration_seconds": round(time.monotonic() - start, 1),
                "jobs": accepted,
                "queue_full_rejections": rejected,
                "disconnect_reconnect_cycles": reconnects,
                "rss_start_mb": samples[0]["rss_mb"],
                "rss_end_mb": samples[-1]["rss_mb"],
                "rss_max_mb": max(v["rss_mb"] for v in samples),
                "cpu_mean_one_core_percent": round(statistics.mean(v["cpu"] for v in samples), 2),
                "snapshot_ms_p95": round(
                    sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 2
                ),
                "fd_start": samples[0]["fds"],
                "fd_end": samples[-1]["fds"],
                "thread_max": max(v["threads"] for v in samples),
                "scope": sys.platform + ", no microphone, models or live accounts",
            }
            serialized = json.dumps(report, ensure_ascii=False, indent=2)
            if args.output:
                args.output.write_text(serialized, encoding="utf-8")
            print(serialized)
        finally:
            try:
                client.call("shutdown")
                process.wait(timeout=15)
            except (OSError, RuntimeError, psutil.NoSuchProcess, psutil.TimeoutExpired):
                if process.is_running():
                    process.terminate()
                    process.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
