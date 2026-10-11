"""Offline conversation contracts: character, session history and quiz turns."""

from types import SimpleNamespace

import pytest

from personality import PERSONALITIES, REPLIES, STYLE_REPLIES, preview, reply
from skills.interactive import (
    QUIZ,
    QUIZ_DETAILS,
    quiz,
    quiz_answer,
    quiz_hint,
    quiz_repeat,
    quiz_stop,
    support,
)


def state(style="friendly", name="Саша"):
    return SimpleNamespace(
        owner_name=name, config_getter=lambda: {"profile": {"personality": style}}
    )


@pytest.mark.parametrize("style", PERSONALITIES)
def test_all_characters_have_authored_core_topics(style):
    for kind in REPLIES:
        if kind == "fact":
            continue
        assert STYLE_REPLIES[style][kind]
        assert "{name}" not in reply(kind, state(style))


@pytest.mark.parametrize("style", PERSONALITIES)
def test_shuffle_cycle_exhausts_pool_and_never_repeats_at_boundary(style):
    session = state(style)
    size = len(STYLE_REPLIES[style]["greeting"])
    responses = [reply("greeting", session) for _ in range(size * 3)]
    for start in range(0, size * 3, size):
        assert len(set(responses[start : start + size])) == size
    assert all(a != b for a, b in zip(responses, responses[1:]))


def test_facts_keep_shared_history_across_personality_switch():
    config = {"profile": {"personality": "friendly"}}
    session = SimpleNamespace(owner_name="", config_getter=lambda: config)
    facts = []
    for index in range(len(REPLIES["fact"])):
        config["profile"]["personality"] = list(PERSONALITIES)[index % 7]
        facts.append(reply("fact", session))
    assert len(set(facts)) == len(REPLIES["fact"])


def test_preview_is_stable_and_does_not_consume_live_cycle():
    session = state()
    assert preview("lively", " Саша ") == preview("lively", "Саша")
    assert "Саша" in preview("lively", "Саша")
    assert "Саша" not in preview("lively")
    assert not hasattr(session, "_reply_cycles")
    assert preview("invalid") == preview("friendly")
    with pytest.raises(ValueError):
        preview("friendly", intent="missing")


def test_state_and_topic_histories_are_independent():
    one, two = state(), state()
    reply("greeting", one)
    assert not hasattr(two, "_reply_cycles")
    greeting_cycle = list(one._reply_cycles[("friendly", "greeting")][0])
    reply("thanks", one)
    assert one._reply_cycles[("friendly", "greeting")][0] == greeting_cycle


@pytest.mark.parametrize("style", PERSONALITIES)
@pytest.mark.parametrize(
    "text,kind",
    [
        ("я устала", "support_tired"),
        ("у меня не получилось", "support_setback"),
        ("мне тревожно", "support_worried"),
        ("поддержи меня", "support"),
    ],
)
def test_support_selects_relevant_situation(style, text, kind):
    session = state(style)
    result = support(session, text)
    assert result
    assert kind in session._last_reply


def test_unknown_personality_and_missing_optional_state_fields():
    assert reply("greeting", state("unknown"))
    assert reply("greeting", SimpleNamespace())
    assert "None" not in reply("greeting", state(name=None))


def test_quiz_no_repeats_after_answers_and_boundary():
    session = state()
    answers = []
    for _ in range(len(QUIZ) + 1):
        quiz(session)
        answer = session._quiz_answer
        answers.append(answer)
        result = quiz_answer(session, answer)
        assert "Верно" in result
        assert QUIZ_DETAILS[answer][0] in result
    assert len(set(answers[:-1])) == len(QUIZ)
    assert answers[-1] != answers[-2]


def test_hint_repeat_stop_preserve_or_clear_pending_turn():
    session = state()
    question = quiz(session)
    expected = session._quiz_answer
    assert quiz_repeat(session) in question
    assert quiz_hint(session) == QUIZ_DETAILS[expected][1]
    assert session._quiz_answer == expected
    assert quiz_repeat(session) in question
    assert "завершена" in quiz_stop(session)
    assert "Сначала" in quiz_answer(session, expected)
    assert "Сначала" in quiz_repeat(session)
    assert "Сначала" in quiz_hint(session)
    assert "нет" in quiz_stop(session)


@pytest.mark.parametrize(
    "expected,answer",
    [
        ("тихий", "Это Тихий океан!"),
        ("шесть", "6"),
        ("восемь", "я думаю, восемь бит"),
        ("девяносто", "90 градусов."),
        ("двоичная", "в двоичной системе"),
    ],
)
def test_quiz_accepts_explicit_alternatives(expected, answer):
    session = state()
    session._quiz_answer = expected
    assert "Верно" in quiz_answer(session, answer)


def test_quiz_rejects_substring_guess_and_does_not_consume_blank():
    session = state()
    session._quiz_answer = "марс"
    assert "Не расслышала" in quiz_answer(session, "... ")
    assert session._quiz_answer == "марс"
    assert "Правильный ответ" in quiz_answer(session, "не марс а юпитер")
    assert session._quiz_answer is None


def test_new_dialogue_commands_route_to_expected_handlers():
    from skill_manager import SkillManager

    manager = SkillManager()
    for text, handler in [
        ("дай подсказку", "quiz_hint"),
        ("повтори вопрос", "quiz_repeat"),
        ("останови викторину", "quiz_stop"),
        ("я волнуюсь", "support"),
        ("у меня не получилось", "support"),
        ("ещё факт", "fact"),
    ]:
        matched = manager.match(text)
        assert matched and matched[0].func.__name__ == handler


@pytest.mark.parametrize("style", PERSONALITIES)
@pytest.mark.parametrize(
    "intent",
    [
        "greeting",
        "thanks",
        "identity",
        "mood",
        "support",
        "conversation",
        "bored",
        "evening",
        "joke",
    ],
)
def test_each_character_core_topic_has_three_distinct_responses(style, intent):
    pool = STYLE_REPLIES[style][intent]
    assert len(pool) >= 3
    assert len(set(pool)) == len(pool)
    session = state(style)
    responses = [reply(intent, session) for _ in range(len(pool))]
    assert len(set(responses)) == len(pool)
    assert preview(style, "Саша", intent) == pool[0].format(name=", Саша")
