"""Keep unrelated work responsive while serializing actions on the same resource."""

LANES = (
    "general",
    "music",
    "volume",
    "discord",
    "desktop",
    "files",
    "web",
    "warframe",
    "system",
    "plugins",
    "sequence",
    "undo",
)

FILES = {
    "skills.system_mon.clean_temp_files",
    "skills.system_mon.organize_downloads",
    "skills.workflows.undo_sort",
    "skills.workflows.preview_sort",
    "skills.windows.empty_recycle_bin",
}
WEB = {
    "skills.webo.get_weather_forecast",
    "skills.webo.search_wikipedia",
    "skills.webo.read_latest_news",
    "skills.utils.convert_currency_or_units",
}
GAME_NETWORK = {
    "briefing",
    "refresh",
    "cycles",
    "events",
    "baro",
    "nightwave",
    "plan",
    "farm",
    "recipe",
    "relics",
    "build",
    "market",
    "show_briefing",
}


def lane_for(entry):
    if entry is None:
        return "general"
    ident, module, name = entry.id, entry.module, entry.func.__name__
    if ident == "skills.workflows.undo_action":
        return "undo"
    if entry.func.__module__.startswith(("plugins.", "krest_imported_")):
        return "plugins"
    if ident in FILES:
        return "files"
    if ident in WEB:
        return "web"
    if module == "discord_voice":
        return "discord"
    if (
        ident == "skills.workflows.app_volume"
        or module == "media"
        and name
        in {
            "set_exact_volume",
            "set_volume_max",
            "volume_up",
            "volume_down",
            "mute_system",
            "unmute_system",
        }
    ):
        return "volume"
    if module == "warframe" and name in GAME_NETWORK:
        return "warframe"
    if module == "system_mon":
        return "system"
    if ident == "skills.workflows.diagnose_player":
        return "music"
    if module == "media" and not name.startswith("open_"):
        return "music"
    if module == "windows" or ident in {
        "skills.workflows.launch_catalog_app",
        "skills.workflows.favourite_player",
        "skills.workflows.favourite_game",
        "skills.webo.launch_steam_game",
        "skills.warframe.launch",
    }:
        return "desktop"
    return "general"
