"""Bounded, deterministic discovery of local speech models; never downloads code."""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def is_vosk(path):
    return (path / "am" / "final.mdl").is_file() and (path / "conf" / "model.conf").is_file()


def discover_models(settings, base_dir=None):
    root = Path(base_dir or BASE_DIR)
    cfg = settings.get("app_settings", {})
    configured = {
        key: root / str(cfg.get(key) or default)
        for key, default in [("vosk_path", "model"), ("silero_path", "model_silero.pt")]
    }
    # Explicit existing choices win. Old absolute paths from another PC can recover.
    directories = [root, root / "model", root / "models", root / "voice", configured["vosk_path"]]
    from app_paths import data_dir

    directories.extend([data_dir(), data_dir() / "models", data_dir() / "model"])
    candidates = []
    for folder in dict.fromkeys(directories):
        try:
            children = sorted(folder.iterdir(), key=lambda p: p.name)[:100]
        except OSError:
            continue
        candidates.append(folder)
        candidates.extend(p for p in children if p.is_dir() and p.name != ".venv")
    candidates = list(dict.fromkeys(candidates))
    vosk = configured["vosk_path"] if is_vosk(configured["vosk_path"]) else None
    if vosk is None:
        ready = [p for p in candidates if is_vosk(p)]
        preferred = [p for p in ready if p == root / "model" or p.parent == root / "model"]
        if len(preferred) == 1:
            vosk = preferred[0]
        elif len(ready) == 1:
            vosk = ready[0]
        elif data_dir() / "models" / "vosk-model-small-ru-0.22" in ready:
            vosk = data_dir() / "models" / "vosk-model-small-ru-0.22"
    silero = configured["silero_path"] if configured["silero_path"].is_file() else None
    if silero is None:
        # Known Silero package names only; don't load arbitrary .pt files.
        ready = []
        for folder in candidates:
            for name in (
                "model_silero.pt",
                "silero.pt",
                "silero-ru.pt",
                "v4_ru.pt",
                "v3_1_ru.pt",
                "v3_ru.pt",
            ):
                path = folder / name
                if path.is_file():
                    ready.append(path)
            ready.extend(path for path in folder.glob("*silero*.pt") if path.is_file())
        ready = list(dict.fromkeys(ready))
        preferred = root / "model_silero.pt"
        if preferred in ready:
            silero = preferred
        elif len(ready) == 1:
            silero = ready[0]
        elif data_dir() / "models" / "v4_ru.pt" in ready:
            silero = data_dir() / "models" / "v4_ru.pt"
    return {
        "vosk_path": str(vosk.resolve()) if vosk else "",
        "silero_path": str(silero.resolve()) if silero else "",
    }
