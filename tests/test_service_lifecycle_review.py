import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bridge_sdk import PREFIX
from integrations import IntegrationManager


def manager(tmp_path):
    return IntegrationManager(SimpleNamespace(config_mgr=SimpleNamespace(snapshot=lambda: {})), tmp_path)


@pytest.mark.parametrize("failure", [OSError("denied"), subprocess.TimeoutExpired("child", 3)])
def test_failed_stop_retains_ownership_and_blocks_restart(tmp_path, failure):
    mgr = manager(tmp_path)
    proc = Mock()
    proc.poll.return_value = None
    if isinstance(failure, OSError):
        proc.terminate.side_effect = failure
    else:
        proc.wait.side_effect = failure
    mgr.processes["test.py"] = proc
    mgr.start = Mock()
    with pytest.raises(RuntimeError, match="повторный запуск"):
        mgr.restart("test.py")
    assert mgr.processes["test.py"] is proc
    mgr.start.assert_not_called()


def test_stop_all_attempts_remaining_children_after_failure(tmp_path):
    mgr = manager(tmp_path)
    stubborn = Mock()
    stubborn.poll.return_value = None
    stubborn.terminate.side_effect = OSError("denied")
    exited = Mock()
    exited.poll.return_value = 0
    mgr.processes.update({"stubborn.py": stubborn, "exited.py": exited})
    with pytest.raises(RuntimeError, match="stubborn.py"):
        mgr.stop_all()
    assert mgr.processes == {"stubborn.py": stubborn}
    exited.stdin.close.assert_called_once()
    assert mgr._closed


def test_details_reads_restart_deadline_once(tmp_path):
    mgr = manager(tmp_path)

    class ConcurrentDeadlines(dict):
        def __contains__(self, key):
            self.pop(key, None)
            return True

    mgr.restart_due = ConcurrentDeadlines({"test.py": 1})
    assert mgr.details("test.py")["restart_in"] == 0


def test_status_does_not_erase_connection_error(tmp_path):
    mgr = manager(tmp_path)
    for event in ({"type": "ready"}, {"type": "error", "text": "offline"}, {"type": "status", "text": "cached"}):
        mgr._event("test.py", PREFIX + json.dumps(event), {})
    assert mgr.errors["test.py"] == "offline"
    assert mgr.bridge["test.py"]["ready"] is False
    mgr._event("test.py", PREFIX + json.dumps({"type": "ready"}), {})
    assert "test.py" not in mgr.errors
    assert mgr.bridge["test.py"]["ready"]


def test_error_can_advertise_refresh_without_false_ready(tmp_path, monkeypatch):
    monkeypatch.setattr("licensing.require_premium", lambda settings: None)
    mgr = manager(tmp_path)
    proc = Mock()
    proc.poll.return_value = None
    mgr.processes['test.py'] = proc
    mgr._event('test.py', PREFIX + json.dumps({'type': 'error', 'text': 'offline', 'capabilities': ['refresh', 'shell']}), {})
    assert mgr.bridge['test.py']['capabilities'] == ['refresh']
    assert not mgr.bridge['test.py']['ready']
    mgr.send('test.py', 'refresh')
    proc.stdin.write.assert_called_once()
    mgr.manifests['test.py'] = {'permissions': []}
    mgr._event('test.py', PREFIX + json.dumps({'type': 'error', 'capabilities': ['refresh']}), {})
    with pytest.raises(RuntimeError):
        mgr.send('test.py', 'refresh')


def test_kernel_shutdown_closes_runtime_when_service_stop_fails(monkeypatch):
    from kernel_service import Kernel

    kernel = Mock()
    kernel.stop.wait.return_value = True
    kernel.integrations.stop_all.side_effect = RuntimeError('child still running')
    monkeypatch.setattr('kernel_service.time.sleep', lambda seconds: None)
    with pytest.raises(RuntimeError, match='child still running'):
        Kernel.pump(kernel)
    kernel.runtime.close.assert_called_once()
