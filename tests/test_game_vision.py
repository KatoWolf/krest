from types import SimpleNamespace
from unittest.mock import MagicMock

from game_vision import GameVision, classify_screen


def test_screen_labels_are_bounded_and_unknown_stays_unknown():
    assert classify_screen("МИССИЯ\nВЫПОЛНЕНА  Награды")["screen"] == "mission_complete"
    assert classify_screen("Foundry")["screen"] == "foundry"
    assert classify_screen("Mission failed")["screen"] == "mission_failed"
    assert classify_screen("Select a reward")["screen"] == "relic_reward"
    assert classify_screen("Неизвестное окно, какие-то ресурсы")["screen"] == "unknown"
    assert classify_screen("myfoundrytest")["screen"] == "unknown"


def test_memory_needs_two_frames_and_never_updates_inventory():
    store = MagicMock()
    vision = GameVision(store, observe=lambda: classify_screen("mission complete"))
    vision.scan()
    store.add.assert_not_called()
    vision.scan()
    store.add.assert_called_once()
    assert store.add.call_args.args == ("journal", "Экран: Миссия завершена")
    vision.scan()
    assert store.add.call_count == 1
    vision.observe = lambda: classify_screen("unknown")
    vision.scan()
    vision.scan()
    vision.observe = lambda: classify_screen("mission complete")
    vision.scan()
    vision.scan()
    assert store.add.call_count == 2


def test_disabled_and_background_windows_do_not_produce_observations():
    store = MagicMock()
    vision = GameVision(store, observe=MagicMock())
    vision.tick(False, True)
    vision.tick(True, False)
    vision.observe.assert_not_called()
    vision.observe = MagicMock(side_effect=ValueError("not foreground"))
    vision.scan()
    assert "активное окно" in vision.snapshot()["status"]
    store.add.assert_not_called()
    vision.observe = lambda: classify_screen("foundry")
    vision.allowed = lambda: False
    vision.scan()
    assert vision.snapshot()["screen"] == "unknown"
    vision.close()
    vision.tick(True, True)
    assert vision.thread is None


def test_failure_has_backoff_and_only_one_scan_runs():
    vision = GameVision(MagicMock(), observe=MagicMock(side_effect=RuntimeError("OCR unavailable")))
    vision.scan()
    assert "OCR unavailable" in vision.snapshot()["status"]
    assert not vision.snapshot()["busy"]
    assert vision.next_scan > 0
    vision.tick(True, True, now=vision.next_scan - 10)
    assert vision.thread is None
    vision.thread = SimpleNamespace(is_alive=lambda: True)
    vision.tick(True, True, now=vision.next_scan + 10)
    assert vision.observe.call_count == 1
