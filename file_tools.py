"""Bounded duplicate inspection and validated sorting policies; never auto-delete."""

from collections import defaultdict
from pathlib import Path


def validate_policy(policy):
    if not isinstance(policy, dict):
        raise ValueError("Политика файлов должна быть объектом")
    folders, exclusions, rules = (policy.get(k, []) for k in ("folders", "exclusions", "rules"))
    if (
        not isinstance(folders, list)
        or len(folders) > 10
        or any(not isinstance(p, str) or not Path(p).is_absolute() for p in folders)
    ):
        raise ValueError("До 10 дополнительных папок с абсолютными путями")
    if (
        not isinstance(exclusions, list)
        or len(exclusions) > 100
        or any(not isinstance(v, str) or not 1 <= len(v) <= 200 for v in exclusions)
    ):
        raise ValueError("До 100 масок исключения файлов")
    if not isinstance(rules, list) or len(rules) > 50:
        raise ValueError("До 50 правил сортировки")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Некорректное правило")
        category = rule.get("category", "")
        if (
            not isinstance(category, str)
            or not category.strip()
            or len(category) > 80
            or any(c in category for c in '/\\:*?"<>|')
            or category in (".", "..")
            or category.endswith((".", " "))
        ):
            raise ValueError("Укажите допустимое имя папки категории")
        extensions = rule.get("extensions")
        if (
            not isinstance(extensions, list)
            or not extensions
            or len(extensions) > 100
            or any(
                not isinstance(v, str) or not v.startswith(".") or len(v) > 20 or v != v.lower()
                for v in extensions
            )
        ):
            raise ValueError('Расширения: список, например [".pdf", ".docx"]')


def find_duplicates(root, stop=None, budget_bytes=512 * 1024 * 1024):
    from sort_history import digest

    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError("Папка не найдена")
    sizes = defaultdict(list)
    errors = []
    count = 0
    # One level of category folders only. No symlink traversal.
    for parent in [root, *(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())]:
        for path in parent.iterdir():
            if stop and stop.is_set():
                return {"groups": [], "errors": errors, "limited": True}
            if path.is_symlink() or not path.is_file() or path.name.startswith("."):
                continue
            count += 1
            if count > 5000:
                break
            try:
                info = path.stat()
                sizes[info.st_size].append((path, info.st_mtime_ns))
            except OSError as exc:
                errors.append(str(exc))
        if count > 5000:
            break
    groups, used, limited = [], 0, count > 5000
    for size, files in sizes.items():
        if len(files) < 2:
            continue
        hashes = defaultdict(list)
        for path, stamp in files:
            if used + size > budget_bytes or (stop and stop.is_set()):
                limited = True
                continue
            used += size
            try:
                value = digest(path)
                info = path.stat()
                if (info.st_size, info.st_mtime_ns) == (size, stamp):
                    hashes[value].append(str(path))
            except OSError as exc:
                errors.append(str(exc))
        groups.extend(
            {"bytes": size, "paths": paths} for paths in hashes.values() if len(paths) > 1
        )
    return {"groups": groups, "errors": errors, "limited": limited}
