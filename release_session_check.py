"""Release checks using local fake download APIs; no live Steam/account requests."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading


def check_sessions():
    from configuration import ConfigManager
    from download_control import LocalDownloader, DownloadControl, DEFAULTS
    from game_phase import GamePhase
    from resource_governor import ResourceGovernor
    from steam_download_control import SteamDownloads

    limits = {"download": 0, "upload": 16384}
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, status=200):
            raw = (json.dumps(value) if isinstance(value, dict) else str(value)).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            endpoint = self.path.rsplit("/", 1)[-1]
            result = {
                "webapiVersion": "2.11.0",
                "speedLimitsMode": "0",
                "downloadLimit": str(limits["download"]),
                "uploadLimit": str(limits["upload"]),
            }
            self.reply(result.get(endpoint, "unknown"), 200 if endpoint in result else 404)

        def do_POST(self):
            from urllib.parse import parse_qs

            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/jsonrpc":
                query = json.loads(data)
                if query["params"][0] != "token:test-secret":
                    return self.reply({"id": "krest", "error": {"code": 1}})
                if query["method"] == "aria2.getGlobalOption":
                    value = {
                        "max-overall-download-limit": str(limits["download"]),
                        "max-overall-upload-limit": str(limits["upload"]),
                    }
                elif query["method"] == "aria2.changeGlobalOption":
                    for key, val in query["params"][1].items():
                        field = "download" if "download" in key else "upload"
                        limits[field] = int(val)
                        calls.append((field, int(val)))
                    value = "OK"
                else:
                    return self.reply({"id": "krest", "error": {"code": 1}})
                return self.reply({"id": "krest", "result": value})
            query = parse_qs(data.decode())
            if self.path.endswith("/login"):
                return self.reply("Ok." if query.get("password") == ["test-password"] else "Fails.")
            field = {
                "/api/v2/transfer/setDownloadLimit": "download",
                "/api/v2/transfer/setUploadLimit": "upload",
            }.get(self.path)
            if field:
                limits[field] = int(query["limit"][0])
                calls.append((field, limits[field]))
                return self.reply("")
            self.reply("", 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with TemporaryDirectory(prefix="krest-session-check-") as folder:
            root = Path(folder)
            cfg = ConfigManager(root / "config.json")
            runtime = SimpleNamespace(
                settings=cfg.snapshot,
                stop_event=threading.Event(),
                performance=SimpleNamespace(game="Dota 2", game_process="dota2.exe", metrics={}),
            )
            for name in ("qbittorrent", "aria2"):
                config = dict(DEFAULTS, client=name, port=server.server_port)
                client = LocalDownloader(
                    config, {"qbittorrent_password": "test-password", "aria2_secret": "test-secret"}
                )
                try:
                    control = DownloadControl(runtime, root)
                    control.reconcile(client, config, True)
                    assert limits == {"download": 262144, "upload": 16384}, limits
                    control.reconcile(client, config, False)
                    assert limits == {"download": 0, "upload": 16384}, limits
                    assert not control.lease
                finally:
                    client.close()
            assert calls == [("download", 262144), ("download", 0)] * 2, calls
            now = [1000.0]
            phase = GamePhase(runtime, root, clock=lambda: now[0])
            value = cfg.snapshot()
            value["download_guard"]["gsi"] = True
            cfg.save_config(value)
            phase.state["files"]["570"] = {"path": "test", "hash": "test"}
            event = {
                "provider": {"appid": 570},
                "map": {"game_state": "DOTA_GAMERULES_STATE_GAME_IN_PROGRESS"},
                "auth": {"token": phase.state["token"]},
            }
            assert not phase.snapshot()["active"]
            assert not phase.receive(dict(event, auth={"token": "incorrect"}))
            assert phase.receive(event) and phase.snapshot()["active"]
            now[0] += 2
            phase.receive(dict(event, map={"game_state": "DOTA_GAMERULES_STATE_POST_GAME"}))
            assert not phase.snapshot()["active"]
            budget = ResourceGovernor(runtime)
            for stamp in (1, 2):
                runtime.performance.metrics = {"sampled_at": stamp, "cpu": 15, "memory_mb": 400}
                budget.tick()
            assert not budget.allowed("vision")[0]
            for stamp in (3, 4, 5):
                runtime.performance.metrics = {"sampled_at": stamp, "cpu": 1, "memory_mb": 400}
                budget.tick()
            assert budget.allowed("vision")[0]
            # Never resume a download that was already paused by its owner.
            rows = {
                "1": {
                    "appid": "1",
                    "name": "One",
                    "path": "one",
                    "active": True,
                    "paused": False,
                    "signature": "active",
                },
                "2": {
                    "appid": "2",
                    "name": "Two",
                    "path": "two",
                    "active": False,
                    "paused": True,
                    "signature": "manual",
                },
            }
            applied = []

            def action(verb, row, identity):
                applied.append((verb, row["appid"]))
                rows[row["appid"]].update(
                    paused=verb == "pause", active=verb != "pause", signature=verb
                )

            steam = SteamDownloads(
                runtime,
                root,
                reader=lambda path: dict(next(r for r in rows.values() if r["path"] == path)),
                scan=lambda _: [dict(r) for r in rows.values() if r["active"]],
                identity=lambda: {"pid": 1, "created": 2},
                action=action,
            )
            steam.reconcile(True)
            steam.reconcile(False)
            assert applied == [("pause", "1"), ("resume", "1")] and rows["2"]["paused"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    print(
        "Session guard: local qBittorrent/aria2 APIs, reversible limits, Steam ownership, match signals and budget hysteresis passed.",
        flush=True,
    )


if __name__ == "__main__":
    check_sessions()
