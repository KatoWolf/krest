"""Warframe and appearance settings, independent of game/network modules."""

DEFAULT_WARFRAME = {
    "enabled": True,
    "platform": "pc",
    "poll_seconds": 120,
    "auto_summary": True,
    "auto_profile": True,
    "start_scenario": "",
    "notifications": True,
    "notify_minutes": 5,
    "cycles": ["cetusCycle"],
    "reward_keywords": "",
    "baro_notify": True,
    "reset_notify": True,
    "market_online_only": True,
    "market_notify": True,
    "overlay_enabled": True,
    "overlay_corner": "top-right",
    "overlay_size": 52,
    "overlay_labels": True,
    "overlay_foreground_only": True,
    "capture_on_bookmark": False,
    "clip_on_bookmark": False,
    "launch_path": "",
    "volume_apps": "",
    "volume_percent": 25,
    "health_notify": True,
    "memory_limit_percent": 90,
    "disk_free_gb": 5,
    "temperature_limit": 85,
}
DEFAULT_EFFECTS = {"mode": "off", "intensity": 2, "pause_in_game": True}


def validate_game_settings(data):
    cfg = data["warframe"]
    for key, default in DEFAULT_WARFRAME.items():
        value = cfg[key]
        if isinstance(default, bool) and type(value) is not bool:
            raise ValueError("Warframe: нужен переключатель " + key)
        if isinstance(default, str) and (not isinstance(value, str) or len(value) > 2000):
            raise ValueError("Warframe: некорректное поле " + key)
    if cfg["platform"] not in ("pc", "ps4", "xb1", "swi"):
        raise ValueError("Выберите платформу Warframe")
    for key, low, high in [
        ("poll_seconds", 60, 900),
        ("notify_minutes", 1, 30),
        ("overlay_size", 32, 96),
        ("volume_percent", 0, 100),
        ("memory_limit_percent", 50, 100),
        ("disk_free_gb", 1, 100),
        ("temperature_limit", 50, 110),
    ]:
        if type(cfg[key]) is not int or not low <= cfg[key] <= high:
            raise ValueError(f"Warframe: {key} должно быть от {low} до {high}")
    if cfg["overlay_corner"] not in ("top-left", "top-right", "bottom-left", "bottom-right"):
        raise ValueError("Выберите угол для индикатора")
    if not isinstance(cfg["cycles"], list) or any(
        v not in ("cetusCycle", "vallisCycle", "cambionCycle", "zarimanCycle")
        for v in cfg["cycles"]
    ):
        raise ValueError("Неизвестный цикл Warframe")
    effects = data["visual_effects"]
    if (
        effects["mode"] not in ("off", "snow", "blood", "embers", "stars")
        or type(effects["intensity"]) is not int
        or not 1 <= effects["intensity"] <= 3
        or type(effects["pause_in_game"]) is not bool
    ):
        raise ValueError("Некорректные параметры эффектов оформления")
