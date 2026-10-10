"""Run Python sources: GUI by default, or diagnostics/one text command via CLI."""

import argparse
import json
import logging
from dataclasses import asdict


def main(argv=None):
    parser = argparse.ArgumentParser(description="КРЕСТ — локальный ассистент")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Проверить загрузку навыков без GUI и внешних запросов",
    )
    parser.add_argument("--check-output", help=argparse.SUPPRESS)
    parser.add_argument("--release-model-check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--release-smoke", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--service", help=argparse.SUPPRESS)
    parser.add_argument("--external-script", help=argparse.SUPPRESS)
    parser.add_argument("--command", help="Выполнить одну текстовую команду")
    parser.add_argument(
        "--voice-core", action="store_true", help="Запустить голосовой режим в консоли"
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Обновить чистую Git-копию исходников при остановленном ядре",
    )
    parser.add_argument(
        "--rollback", action="store_true", help="Вернуть предыдущую версию исходников"
    )
    parser.add_argument("--quick", action="store_true", help="Открыть быструю панель")
    parser.add_argument("--restore", help="Восстановить локальную копию при остановленном ядре")
    parser.add_argument("--kernel", action="store_true", help="Постоянное ядро без меню")
    parser.add_argument("--config", help="Путь к конфигурации")
    parser.add_argument("--game-overlay", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--safe-mode", action="store_true", help="Без автозапуска ботов")
    parser.add_argument("--status", action="store_true", help="Состояние работающего ядра")
    parser.add_argument("--stop-kernel", action="store_true", help="Завершить ядро и его сервисы")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    import os

    if args.config:
        os.environ["KREST_CONFIG_PATH"] = str(__import__("pathlib").Path(args.config).resolve())
    from app_paths import data_dir

    data_dir().mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("KREST_DATA_DIR", str(data_dir()))
    if args.service or args.external_script:
        from service_runner import run_service, run_external

        return run_service(args.service) if args.service else run_external(args.external_script)
    from configuration import CONFIG_PATH, ConfigManager
    from app_runtime import AssistantRuntime

    settings = ConfigManager(args.config or CONFIG_PATH)
    if args.release_model_check:
        from release_smoke import check_models

        check_models(settings.snapshot())
        return 0
    if args.release_smoke:
        from release_smoke import check_kernel

        check_kernel(settings.filepath)
        return 0
    if args.game_overlay:
        from game_overlay import run_overlay

        return run_overlay(settings.filepath)
    if args.update or args.rollback:
        from source_update import SourceUpdater
        from pathlib import Path

        try:
            updater = SourceUpdater(Path(__file__).parent, settings)
            print(updater.rollback() if args.rollback else updater.apply())
            return 0
        except (OSError, ValueError) as exc:
            print("Обновление не выполнено: " + str(exc))
            return 1
    if args.restore:
        from backup_service import restore_backup

        try:
            print("Восстановлено. Копия для отката: " + restore_backup(args.restore, settings))
            return 0
        except (OSError, ValueError) as exc:
            print("Восстановление не выполнено: " + str(exc))
            return 1
    if args.kernel:
        from kernel_service import serve

        return serve(settings.filepath, args.safe_mode)
    if args.status or args.stop_kernel:
        from kernel_client import KernelClient

        try:
            print(
                json.dumps(
                    KernelClient(settings.filepath).call(
                        "shutdown" if args.stop_kernel else "ping"
                    ),
                    ensure_ascii=False,
                )
            )
            return 0
        except (OSError, ValueError, RuntimeError) as exc:
            print("Ядро недоступно: " + str(exc))
            return 1
    if not (args.check or args.command or args.voice_core):
        from welcome_gui import open_menu_gui

        from kernel_client import KernelClient, RemoteRuntime
        from process_lock import ProcessLock
        import voice

        client = KernelClient(settings.filepath).ensure_started(args.safe_mode)
        lock = ProcessLock(settings.filepath.parent / ".ui.lock")
        if not lock.acquire():
            client.call("ui_open", quick=args.quick)
            return 0
        remote = RemoteRuntime(client, settings.snapshot)
        voice.set_remote(remote)
        try:
            open_menu_gui(remote=remote, config_path=settings.filepath, quick=args.quick)
        finally:
            remote.detach()
            lock.close()
        return 0
    if args.check:
        from skill_manager import SkillManager

        manager = SkillManager(settings.snapshot)
        if args.check_output:
            from storage import atomic_write_json

            atomic_write_json(
                args.check_output, {"skills": len(manager.catalog()), "errors": manager.errors}
            )
        print(
            json.dumps(
                {"skills": len(manager.catalog()), "errors": manager.errors},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if manager.errors else 0
    from process_lock import ProcessLock

    lock = ProcessLock(settings.filepath.parent / ".kernel.lock")
    if not lock.acquire():
        from kernel_client import KernelClient

        client = KernelClient(settings.filepath)
        try:
            if args.voice_core:
                client.call("voice.start")
                print("Голос включён в работающем ядре.")
                return 0
            import uuid
            import time

            request_key = uuid.uuid4().hex
            if not client.call("submit", text=args.command, request_key=request_key):
                print("Ядро не приняло команду")
                return 1
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                job = next(
                    (r for r in client.call("jobs") if r["request_key"] == request_key), None
                )
                if job and job["status"] not in ("queued", "running"):
                    print(job["result"])
                    return 0 if job["status"] == "completed" else 1
                time.sleep(0.15)
            print("Действие ещё выполняется. Проверьте журнал ядра перед повтором.")
            return 1
        except (OSError, ValueError, RuntimeError) as exc:
            print("Ошибка ядра: " + str(exc))
            return 1
    runtime = None
    try:
        runtime = AssistantRuntime(settings.snapshot, data_dir=settings.filepath.parent)
        if args.command:
            result = runtime.execute(args.command)
            print(result.text)
            return 0 if result.success else 1
        runtime.start()
        runtime.start_voice()
        while not runtime.state.shutdown_requested.wait(0.5):
            if runtime._voice_thread and not runtime._voice_thread.is_alive():
                return 1
    except KeyboardInterrupt:
        pass
    finally:
        if runtime:
            runtime.close()
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
