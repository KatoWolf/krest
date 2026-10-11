# Source imports are analyzed before the optional PyArmor SpecRepacker replacement.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_dynamic_libs

root = Path(os.environ["KREST_BUILD_ROOT"])
own_modules = [p.stem for p in root.glob("*.py") if p.stem != "main"]
hidden = own_modules + ["pystray._win32", "pyautogui._pyautogui_win"]
for package in ("skills", "vosk", "winsdk", "pywinauto", "pycaw", "comtypes", "telebot", "discord", "pyrogram"):
    hidden += collect_submodules(package, filter=lambda name: not any(part in ("tests", "test") for part in name.split(".")))

a = Analysis(
    [str(root / "main.py")], pathex=[str(root)],
    binaries=collect_dynamic_libs("vosk"),
    datas=[(str(root / "assets"), "assets"), (str(root / "license_public_key.pem"), "."), (str(root / "packaging/NOTICE.txt"), ".")],
    hiddenimports=hidden, hookspath=[], runtime_hooks=[],
    excludes=["pytest", "_pytest", "tests", "ruff", "nuitka", "pyarmor", "PyInstaller"],
    noarchive=False,
)
# Hooks may supply documentation/test datasets from third-party packages. Strip
# those data files, while preserving runtime Python modules and required licenses.
a.datas = [row for row in a.datas if not any(p in {"tests", "docs", "__pycache__", ".git", ".github"} for p in Path(row[0]).parts)]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True, name="Krest",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, hide_console="hide-early", disable_windowed_traceback=False,
    icon=str(root / "assets/krest.ico"),
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="Krest")
