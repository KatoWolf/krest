"""Bounded discovery of drop-in Vosk/Silero models beside sources or the EXE."""

from pathlib import Path
import zipfile

BASE_DIR = Path(__file__).resolve().parent
TTS_NAMES = ("model_silero.pt", "silero.pt", "silero-ru.pt", "v4_ru.pt", "v3_1_ru.pt", "v3_ru.pt")
SKIP = {".git", ".venv", ".build-venv", "node_modules", "__pycache__", "build", "dist"}


def is_vosk(path):
    return (path / "am" / "final.mdl").is_file() and (path / "conf" / "model.conf").is_file()


def is_silero(path):
    """Check the Torch package index without unpickling or importing a model."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            return any(name.endswith("/tts_models/model.pkl") for name in names) and any(
                name.endswith("/.data/version") for name in names
            )
    except (OSError, ValueError, zipfile.BadZipFile):
        return False


def _folders(root, max_depth=3, budget=200):
    """Look inside model containers, never scan the whole disk or follow links."""
    pending = [(Path(root), 0)]
    result = []
    while pending and len(result) < budget:
        folder, depth = pending.pop(0)
        if folder.is_symlink():
            continue
        try:
            if not folder.is_dir():
                continue
            result.append(folder)
            if depth < max_depth and not is_vosk(folder):
                children = sorted(folder.iterdir(), key=lambda p: p.name.casefold())[:100]
                pending.extend(
                    (p, depth + 1)
                    for p in children
                    if p.name not in SKIP and p.is_dir() and not p.is_symlink()
                )
        except OSError:
            continue
    return result


def discover_models(settings, base_dir=None):
    from app_paths import compiled, data_dir, program_dir

    # A frozen __file__ points into _internal/_MEIPASS. User model/ lives beside EXE.
    primary = Path(base_dir) if base_dir is not None else program_dir() if compiled() else BASE_DIR
    roots = list(dict.fromkeys([primary, BASE_DIR if base_dir is None else primary, data_dir()]))
    cfg = settings.get("app_settings", {})
    configured = {
        key: primary / str(cfg.get(key) or default)
        for key, default in (("vosk_path", "model"), ("silero_path", "model_silero.pt"))
    }
    candidates = []
    for root in roots:
        candidates.append(root)
        for container in ("model", "models", "voice"):
            candidates.extend(_folders(root / container))
        # Also support a unpacked vosk-model-small-* folder directly beside the app.
        try:
            candidates.extend(
                p
                for p in sorted(root.iterdir())[:100]
                if p.is_dir() and not p.is_symlink() and is_vosk(p)
            )
        except OSError:
            pass
    candidates.extend(_folders(configured["vosk_path"]))
    candidates = list(dict.fromkeys(candidates))
    vosk = configured["vosk_path"] if is_vosk(configured["vosk_path"]) else None
    if vosk is None:
        ready = [p for p in candidates if is_vosk(p)]
        local = [p for p in ready if p == primary / "model" or primary / "model" in p.parents]
        small = [p for p in local if "small" in p.name.casefold()]
        if len(local) == 1:
            vosk = local[0]
        elif len(small) == 1:
            vosk = small[0]
        elif len(ready) == 1:
            vosk = ready[0]
        elif data_dir() / "models/vosk-model-small-ru-0.22" in ready:
            vosk = data_dir() / "models/vosk-model-small-ru-0.22"
    silero = configured["silero_path"] if is_silero(configured["silero_path"]) else None
    if silero is None:
        ready = []
        for folder in candidates:
            ready.extend(folder / name for name in TTS_NAMES if is_silero(folder / name))
            ready.extend(p for p in folder.glob("*silero*.pt") if is_silero(p))
        ready = list(dict.fromkeys(ready))
        local = [
            p for p in ready if p == primary / "model_silero.pt" or primary / "model" in p.parents
        ]
        if primary / "model_silero.pt" in ready:
            silero = primary / "model_silero.pt"
        elif len(local) == 1:
            silero = local[0]
        elif len(ready) == 1:
            silero = ready[0]
        elif data_dir() / "models/v4_ru.pt" in ready:
            silero = data_dir() / "models/v4_ru.pt"
    return {
        "vosk_path": str(vosk.resolve()) if vosk else "",
        "silero_path": str(silero.resolve()) if silero else "",
    }
