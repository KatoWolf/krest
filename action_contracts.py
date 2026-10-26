"""Explicit scheduling contracts for known skills; no guessed capabilities."""

from command_lanes import lane_for

TRANSIENT = {
    "skills.media." + name
    for name in (
        "next_track",
        "prev_track",
        "pause_music",
        "play_music",
        "like_track",
        "unlike_track",
        "volume_up",
        "volume_down",
        "set_exact_volume",
        "mute_system",
        "unmute_system",
    )
} | {
    "skills.discord_voice.mute_microphone",
    "skills.discord_voice.unmute_microphone",
    "skills.windows.take_screenshot",
}

# Deferred work must not depend on a transient selected window, cursor or song.
DEFERRED = {
    "skills.basic.basic",
    "skills.timers_alarms.set_timer",
    "skills.timers_alarms.named_reminder",
    "skills.webo.get_weather_forecast",
    "skills.workflows.launch_catalog_app",
    "skills.webo.launch_steam_game",
    "skills.workflows.favourite_player",
    "skills.workflows.favourite_game",
    "skills.media.open_yandex_music",
    "skills.media.open_spotify",
    "skills.autonomy.duty_backup",
    "skills.autonomy.duty_health",
    "skills.autonomy.verify_backups",
}
IDLE = {name for name in DEFERRED if name.startswith("skills.autonomy.")}


def contract(entry):
    if entry is None:
        return {"lane": "general", "transient": False, "deferred": False, "idle": False}
    return {
        "lane": lane_for(entry),
        "transient": entry.id in TRANSIENT,
        "deferred": entry.id in DEFERRED,
        "idle": entry.id in IDLE,
    }
