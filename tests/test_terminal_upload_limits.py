"""Actual downloaded bytes enforce the existing upload limit before any write."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def upload(tmp_path, monkeypatch):
    import configuration
    import krest_bot

    cfg = {"api_keys": {"telegram_owner_id": "42"}, "remote_control": {"allow_uploads": True}}
    monkeypatch.setattr(
        configuration, "ConfigManager", lambda: SimpleNamespace(snapshot=lambda: cfg)
    )
    monkeypatch.setattr(krest_bot, "UPLOADS_DIR", str(tmp_path))
    client = SimpleNamespace(
        get_file=Mock(),
        download_file=Mock(),
        send_chat_action=Mock(),
        reply_to=Mock(),
    )
    monkeypatch.setattr(krest_bot.bot, "client", client)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(id=42, type="private"),
        document=SimpleNamespace(file_name="allowed.bin", file_id="offline"),
        photo=None,
        video=None,
        audio=None,
    )
    return krest_bot, client, message, tmp_path


@pytest.mark.parametrize("declared", [None, 0, 1024])
def test_actual_oversize_rejected_without_writing(upload, declared):
    module, client, message, root = upload
    client.get_file.return_value = SimpleNamespace(file_size=declared, file_path="offline")
    client.download_file.return_value = b"x" * (20 * 1024 * 1024 + 1)
    module.handle_remote_upload(message)
    assert not list(root.iterdir())
    assert "Фактический размер" in client.reply_to.call_args.args[1]


@pytest.mark.parametrize("metadata", [{}, {"file_size": None}, {"file_size": 0}, {"file_size": 3}])
def test_allowed_bytes_work_with_optional_size_metadata(upload, metadata):
    module, client, message, root = upload
    client.get_file.return_value = SimpleNamespace(file_path="offline", **metadata)
    client.download_file.return_value = b"abc"
    module.handle_remote_upload(message)
    assert (root / "allowed.bin").read_bytes() == b"abc"


@pytest.mark.parametrize("declared", [-1, "3", True, 1.5, 20 * 1024 * 1024 + 1])
def test_invalid_or_oversize_metadata_rejected_before_download(upload, declared):
    module, client, message, root = upload
    client.get_file.return_value = SimpleNamespace(file_path="offline", file_size=declared)
    module.handle_remote_upload(message)
    client.download_file.assert_not_called()
    assert not list(root.iterdir())


def test_upload_size_change_does_not_expand_owner_access(upload):
    module, client, message, root = upload
    message.from_user.id = 41
    module.handle_remote_upload(message)
    client.get_file.assert_not_called()
    client.download_file.assert_not_called()
    assert not list(root.iterdir())
