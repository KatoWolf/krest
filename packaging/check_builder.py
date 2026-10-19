"""Failure recovery with temporary artifacts; no models or external accounts."""

from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_windows as builder
from build_bootstrap import prepare_environment, environment_valid
from build_support import BuildSession


class ReleaseRecovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="krest-build-check-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stage = self.root / "stage" / "Krest"
        self.stage.mkdir(parents=True)
        (self.stage / "Krest.exe").write_bytes(b"test-only release content")
        self.output = self.root / "dist"
        self.old = self.output / "Krest"
        self.old.mkdir(parents=True)
        (self.old / "keep.txt").write_text("previous version", encoding="utf-8")
        self.archive = self.output / ("Krest-Windows-" + builder.VERSION + ".zip")
        self.archive.write_bytes(b"previous archive")

    def assert_previous(self):
        self.assertEqual((self.old / "keep.txt").read_text(), "previous version")
        self.assertEqual(self.archive.read_bytes(), b"previous archive")
        self.assertTrue((self.stage / "Krest.exe").is_file())
        self.assertEqual(list(self.output.glob("*.partial")), [])

    def test_write_failure_preserves_previous_folder_and_zip(self):
        with patch.object(zipfile.ZipFile, "write", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(RuntimeError, "Прежние файлы сохранены"):
                builder.publish_distribution(self.stage, self.output)
        self.assert_previous()

    def test_failed_crc_never_replaces_release(self):
        with patch.object(zipfile.ZipFile, "testzip", return_value="Krest/Krest.exe"):
            with self.assertRaisesRegex(RuntimeError, "Повреждён"):
                builder.publish_distribution(self.stage, self.output)
        self.assert_previous()

    def test_each_failed_move_rolls_back(self):
        original = Path.rename
        # Old folder, old ZIP, new folder, new ZIP: fail each once, then let rollback run.
        for failed in range(1, 5):
            calls = 0

            def rename(path, target):
                nonlocal calls
                calls += 1
                if calls == failed:
                    raise PermissionError("file is open")
                return original(path, target)

            with self.subTest(move=failed), patch.object(Path, "rename", rename):
                with self.assertRaisesRegex(RuntimeError, "Прежние файлы сохранены"):
                    builder.publish_distribution(self.stage, self.output)
            self.assert_previous()

    def test_success_preserves_old_folder_and_publishes_matching_archive(self):
        target, archive = builder.publish_distribution(self.stage, self.output)
        with zipfile.ZipFile(archive) as zipped:
            self.assertIsNone(zipped.testzip())
            self.assertEqual(zipped.read("Krest/Krest.exe"), (target / "Krest.exe").read_bytes())
        backups = list(self.output.glob("Krest-previous-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "keep.txt").read_text(), "previous version")
        self.assertFalse(self.stage.exists())

    def test_builder_lock_does_not_truncate_active_log(self):
        with BuildSession(self.root):
            print("Первый сборщик")
            log = self.root / "build" / "build-last.log"
            before = log.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "уже выполняется"):
                with BuildSession(self.root):
                    self.fail("second builder entered")
            self.assertEqual(log.read_bytes(), before)
        with BuildSession(self.root):
            print("Второй запуск после завершения")
        self.assertIn(
            "Первый сборщик", log.with_name("build-previous.log").read_text(encoding="utf-8")
        )

    def test_failed_child_is_logged_and_exit_status_retained(self):
        with BuildSession(self.root):
            with self.assertRaises(subprocess.CalledProcessError) as error:
                builder.run_checked(
                    [sys.executable, "-c", "print('builder failure'); raise SystemExit(7)"]
                )
            self.assertEqual(error.exception.returncode, 7)
        self.assertIn("builder failure", (self.root / "build/build-last.log").read_text())

    @unittest.skipUnless(os.name == "nt", "Windows build environment")
    def test_repairs_broken_environment_in_unicode_path(self):
        folder = self.root / "Крест сборка"
        broken = folder / ".build-venv"
        broken.mkdir(parents=True)
        (broken / "keep.txt").write_text("old cache")
        python = prepare_environment(folder, sys.executable, dict(os.environ))
        self.assertTrue(environment_valid(python))
        self.assertEqual(prepare_environment(folder, sys.executable, dict(os.environ)), python)
        backups = list(folder.glob(".build-venv-broken-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / "keep.txt").read_text(), "old cache")
        # A caller running inside that cache would keep python.exe locked during
        # repair. Exercise the actual batch selector with a venv as first choice.
        packaging = folder / "packaging"
        packaging.mkdir()
        shutil.copy2(Path(__file__).with_name("find_python.bat"), packaging)
        # .venv is the selector's first candidate, independent of machines which
        # also have Krest's per-user Python installed.
        (folder / ".build-venv").rename(folder / ".venv")
        script = folder / "check.bat"
        script.write_text(
            '@echo off\ncall "%~dp0packaging\\find_python.bat"\n'
            "if errorlevel 1 exit /b 1\n"
            '"%KREST_PYTHON%" %KREST_PY_ARGS% -c "import sys; assert sys.prefix == sys.base_prefix"\n',
            encoding="utf-8",
        )
        subprocess.run(["cmd", "/d", "/c", str(script)], check=True, timeout=60)


if __name__ == "__main__":
    unittest.main()
