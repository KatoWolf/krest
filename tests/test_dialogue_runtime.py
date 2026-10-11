from unittest.mock import Mock

import pytest

from app_runtime import AssistantRuntime
from configuration import ConfigManager
from personality import PERSONALITIES
from skill_manager import CommandResult


@pytest.fixture
def session(tmp_path, monkeypatch):
    cfg = ConfigManager(tmp_path / 'config.json')
    monkeypatch.setattr('voice.speak', lambda text: None)
    runtime = AssistantRuntime(cfg.snapshot, data_dir=tmp_path)
    yield runtime, cfg
    runtime.close()


def test_repeat_reads_previous_answer_without_repeating_action(session):
    runtime, _ = session
    first = runtime.execute('таймер на пять минут')
    timers = runtime.state.snapshot()['active_timers']
    second = runtime.execute('повтори ответ')
    assert first.success and second.success and first.text == second.text
    assert runtime.state.snapshot()['active_timers'] == timers
    assert not second.skill_id


def test_repeat_expires_and_remote_cannot_read_local_reply(session, monkeypatch):
    runtime, cfg = session
    runtime.execute('расскажи факт')
    monkeypatch.setattr('app_runtime.time.monotonic', lambda: 1000.0)
    runtime._last_local_reply = (879.0, 'private previous response')
    assert not runtime.execute('повтори ответ').success
    runtime._last_local_reply = (1000.0, 'private previous response')
    result = runtime.execute('повтори ответ', source='remote')
    assert not result.success and 'private previous response' not in result.text


@pytest.mark.parametrize('age,expected', [(119.999, True), (120.0, False), (120.001, False), (-0.001, False)])
def test_repeat_ttl_boundary_uses_controlled_clock(session, monkeypatch, age, expected):
    runtime, _ = session
    monkeypatch.setattr('app_runtime.time.monotonic', lambda: 1000.0)
    runtime._last_local_reply = (1000.0 - age, 'private previous response')
    result = runtime.execute('повтори ответ')
    assert result.success is expected
    assert ('private previous response' in result.text) is expected


@pytest.mark.parametrize('style', PERSONALITIES)
def test_unknown_and_repeat_do_not_dispatch_suggestion(session, style):
    runtime, cfg = session
    settings = cfg.snapshot(); settings['profile']['personality'] = style; cfg.save_config(settings)
    entry = next(e for e in runtime.manager.catalog() if e.id == 'skills.interactive.fact')
    original = entry.func; entry.func = Mock(side_effect=AssertionError('fuzzy dispatch'))
    try:
        first = runtime.execute('расскажи фат')
        second = runtime.execute('расскажи фат')
        assert not first.success and not second.success
        assert first.text != second.text
        assert 'расскажи факт' in first.text
        entry.func.assert_not_called()
    finally:
        entry.func = original


def test_short_topic_continues_but_action_ends_context(session):
    runtime, _ = session
    first = runtime.execute('расскажи факт')
    follow = runtime.execute('а ещё')
    assert follow.success and follow.text != first.text
    runtime.execute('15 процентов от 2400')
    assert not runtime.execute('а ещё').success


def test_exact_skill_takes_priority_over_dialogue(session, monkeypatch):
    runtime, _ = session
    runtime.execute('расскажи факт')
    original = runtime.manager.execute
    monkeypatch.setattr(runtime.manager, 'execute', lambda text, state: CommandResult(True, True, 'explicit skill', 'plugin.example') if text == 'ещё' else original(text, state))
    assert runtime.execute('ещё').text == 'explicit skill'


@pytest.mark.parametrize('change', ['disable', 'allowlist', 'lock'])
def test_queued_remote_command_rechecks_current_policy(session, monkeypatch, change):
    runtime, cfg = session
    settings = cfg.snapshot(); settings['remote_control']['enabled'] = True
    cfg.save_config(settings)
    monkeypatch.setattr('remote_policy.session_locked', lambda: False)
    assert runtime.submit('время', source='remote')
    if change == 'disable': settings['remote_control']['enabled'] = False
    if change == 'allowlist': settings['remote_control']['allowed_commands'] = []
    if change == 'lock': monkeypatch.setattr('remote_policy.session_locked', lambda: True)
    cfg.save_config(settings)
    runtime.manager.execute = Mock(side_effect=AssertionError('permission revoked'))
    text, source, _, job_id = runtime.requests.get_nowait()
    runtime._job_local.current = job_id
    assert not runtime.execute(text, source).success
    runtime.requests.task_done()
    runtime.manager.execute.assert_not_called()


def test_remote_transport_alias_rechecked_before_translation(session, monkeypatch):
    runtime, cfg = session
    settings = cfg.snapshot(); settings['remote_control'].update(enabled=True, allowed_commands=['плей'])
    cfg.save_config(settings)
    monkeypatch.setattr('remote_policy.session_locked', lambda: False)
    runtime.manager.execute = Mock(return_value=CommandResult(True, True, 'ok'))
    assert runtime.execute('плей', source='remote').success
    runtime.manager.execute.assert_called_once_with('включи музыку', runtime.state, normalize=False)
    runtime.manager.execute.reset_mock()
    assert not runtime.execute('включи музыку', source='remote').success
    runtime.manager.execute.assert_not_called()


def test_disabling_conversation_module_stops_short_continuation(session):
    runtime, cfg = session
    runtime.execute('расскажи факт')
    settings = cfg.snapshot(); settings['skills']['interactive'] = False; cfg.save_config(settings)
    assert not runtime.execute('ещё').success


def test_oversized_remote_command_rejected_before_dispatch(session):
    runtime, _ = session
    runtime.manager.execute = Mock(side_effect=AssertionError('oversized dispatch'))
    result = runtime.execute('x' * 2001, source='remote')
    assert not result.success and len(result.text) < 200
    runtime.manager.execute.assert_not_called()


def test_remote_custom_alias_cannot_expand_allowlist(session, monkeypatch):
    runtime, cfg = session
    settings = cfg.snapshot()
    settings['remote_control'].update(enabled=True, allowed_commands=['время'])
    settings['command_aliases'] = {'время': 'таймер на пять минут'}
    cfg.save_config(settings)
    monkeypatch.setattr('remote_policy.session_locked', lambda: False)
    assert not runtime.execute('время', source='remote').success
    assert not runtime.state.snapshot()['active_timers']
    # Local aliases retain their previous behavior.
    assert runtime.execute('время').success
    assert len(runtime.state.snapshot()['active_timers']) == 1


def test_scenario_conversation_does_not_create_local_topic(session):
    runtime, _ = session
    assert runtime.execute('расскажи факт', source='scenario').success
    assert not runtime.execute('ещё').success


def test_external_conversation_preserves_recent_local_topic(session):
    runtime, _ = session
    runtime.execute('расскажи шутку')
    topic = runtime.state._dialogue_topic
    runtime.execute('расскажи факт', source='scenario')
    assert runtime.state._dialogue_topic == topic


def test_remote_cannot_answer_local_destructive_clarification(session, monkeypatch):
    runtime, cfg = session
    settings = cfg.snapshot(); settings['remote_control'].update(enabled=True, allowed_commands=['время'])
    cfg.save_config(settings)
    monkeypatch.setattr('remote_policy.session_locked', lambda: False)
    runtime.execute('таймер на пять минут')
    item = runtime.state.snapshot()['active_timers'][0]
    runtime.context.pending = {'created': __import__('time').monotonic(), 'action': 'cancel', 'seconds': 0, 'choices': [dict(item, kind='active_timers', label='время')]}
    before = runtime.state.snapshot()['active_timers']
    result = runtime.execute('время', source='remote')
    assert result.success and result.skill_id != ''
    assert runtime.state.snapshot()['active_timers'] == before
    assert runtime.context.pending is not None


def test_remote_cannot_confirm_local_game_guide_prompt(session, monkeypatch):
    from skills import gid

    runtime, cfg = session
    settings = cfg.snapshot(); settings['remote_control'].update(enabled=True, allowed_commands=['да'])
    cfg.save_config(settings)
    monkeypatch.setattr('remote_policy.session_locked', lambda: False)
    monkeypatch.setattr(gid.dota_state, 'waiting_for_user_answer', True)
    runtime.manager.execute = Mock(return_value=CommandResult(False, False, 'unknown'))
    assert not runtime.execute('да', source='remote').success
    runtime.manager.execute.assert_called_once_with('да', runtime.state, normalize=False)


def test_successful_context_action_ends_conversation_topic(session):
    runtime, _ = session
    runtime.state.add_timer(300, 'чай')
    assert runtime.execute('расскажи факт').success
    assert runtime.execute('отмени его').success
    assert not runtime.state.snapshot()['active_timers']
    assert not runtime.execute('ещё').success
