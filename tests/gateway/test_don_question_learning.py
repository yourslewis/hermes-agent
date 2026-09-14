"""Don gets the same optional learner without opening other profiles or scopes."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest


def configure(tmp_path, profile='don'):
    from agent.interview_notes_bank import initialize_notes_bank
    from agent.interview_questions import load_question_bank
    home = tmp_path / profile
    home.mkdir()
    root = tmp_path / 'bank'
    initialize_notes_bank(root, load_question_bank(tmp_path / 'unconfigured'))
    config = dict(enabled=True, profile=profile, owner='U', team='T', channels=['C'],
                  since='1.0', notes_root=str(root))
    (home / 'question-learning.json').write_text(json.dumps(config))
    return home, root


@pytest.mark.asyncio
async def test_don_correction_roundtrip_and_pause(tmp_path):
    from agent.interview_learning import learn_message
    from agent.interview_questions import load_question_bank
    from gateway.interview_learning import learning_control
    home, root = configure(tmp_path)
    source = dict(profile='don', user_id='U', team='T', channel='C')
    item = dict(question='How should late events be handled?', category='streaming',
                ask_when='designing event processing', skip_when='already specified', resolves='late event policy')
    completion = AsyncMock(return_value={'choices':[{'message':{'content':json.dumps(item)}}]})
    result = await learn_message(home, source, 'You forgot to ask about late events.', '2.0', completion=completion)
    assert result['status'] == 'added', result
    assert any(q['question'] == item['question'] for q in load_question_bank(home)['questions'])
    assert any('auto-generated' in p.read_text() for p in root.glob('*.md'))
    control_source = SimpleNamespace(user_id='U', scope_id='T', chat_id='C')
    assert 'paused' in await learning_control(home, control_source, 'pause')
    result = await learn_message(home, source, 'Remember to ask about scope.', '3.0', completion=completion)
    assert result['status'] == 'ignored'
    assert completion.await_count == 1
    assert any(q['question'] == item['question'] for q in load_question_bank(home)['questions'])


@pytest.mark.asyncio
@pytest.mark.parametrize('field,value', [('profile','rex'),('profile','chloe'),('user_id','OTHER'),('team','OTHER'),('channel','OTHER')])
async def test_don_rejects_other_scope_without_model_call(tmp_path, field, value):
    from agent.interview_learning import learn_message
    home, _ = configure(tmp_path)
    source = dict(profile='don', user_id='U', team='T', channel='C')
    source[field] = value
    completion = AsyncMock()
    result = await learn_message(home, source, 'Remember to ask about scope.', '2.0', completion=completion)
    assert result['status'] == 'ignored'
    completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_don_capture_and_digest(tmp_path, monkeypatch):
    from gateway.interview_learning import capture_learning
    from gateway.question_learning_digest import deliver_digest
    home, _ = configure(tmp_path)
    monkeypatch.setattr('gateway.interview_learning.get_hermes_home', lambda: home)
    learner = AsyncMock()
    monkeypatch.setattr('gateway.interview_learning.learn_message', learner)
    source = SimpleNamespace(user_id='U', scope_id='T', chat_id='C', thread_id='thread')
    adapter = SimpleNamespace(_event_declares_bot_sender=lambda e: False,
        _resolve_user_is_bot=AsyncMock(return_value=False), send=AsyncMock(return_value=SimpleNamespace(success=True)))
    await capture_learning(adapter, {'user':'U','ts':'2.0'}, source, 'Remember to ask about scope.')
    learner.assert_awaited_once()
    assert learner.await_args.args[1]['profile'] == 'don'
    with sqlite3.connect(home/'question-learning.sqlite3') as db:
        db.execute('CREATE TABLE events(event_id TEXT,state TEXT,question_id TEXT,created REAL,delivered INTEGER)')
        db.execute("INSERT INTO events VALUES('event','added','question',1,0)")
    await deliver_digest(adapter, home, source, now=90000)
    adapter.send.assert_awaited_once()
    assert adapter.send.await_args.args[0] == 'C'


@pytest.mark.asyncio
async def test_unapproved_profile_still_ignored(tmp_path):
    from agent.interview_learning import learn_message
    home, _ = configure(tmp_path, 'chloe')
    completion = AsyncMock()
    result = await learn_message(home, dict(profile='chloe',user_id='U',team='T',channel='C'),
        'Remember to ask about scope.', '2.0', completion=completion)
    assert result['status'] == 'ignored'
    completion.assert_not_awaited()
