"""Learning sees only canonical, admitted owner messages, never hydrated context."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

@pytest.mark.asyncio
async def test_capture_uses_canonical_source_and_does_not_change_event(tmp_path, monkeypatch):
    from gateway.interview_learning import capture_learning
    from gateway.config import Platform
    from gateway.session import SessionSource
    seen = []
    async def learn(home, source, text, message_id, internal=False):
        seen.append((source, text, message_id, internal))
        return {'status': 'added'}
    source = SessionSource(platform=Platform.SLACK, chat_id='C', user_id='U', thread_id='th', scope_id='T')
    monkeypatch.setattr('gateway.interview_learning.learn_message', learn)
    monkeypatch.setattr('gateway.interview_learning.get_hermes_home', lambda: tmp_path / 'rex')
    adapter = SimpleNamespace(_event_declares_bot_sender=lambda e: False, _resolve_user_is_bot=AsyncMock(return_value=False))
    event = {'ts': '123.45', 'user': 'U', 'text': 'You forgot to ask about retention.'}
    await capture_learning(adapter, event, source, event['text'])
    assert seen[0][0]['profile'] == 'rex'
    assert seen[0][0]['user_id'] == 'U'
    assert seen[0][1] == event['text']

@pytest.mark.asyncio
async def test_capture_failure_cannot_break_interview(tmp_path, monkeypatch):
    from gateway.interview_learning import capture_learning
    from gateway.config import Platform
    from gateway.session import SessionSource
    async def fail(*args, **kwargs):
        raise ValueError('private detail')
    monkeypatch.setattr('gateway.interview_learning.learn_message', fail)
    monkeypatch.setattr('gateway.interview_learning.get_hermes_home', lambda: tmp_path / 'rex')
    source = SessionSource(platform=Platform.SLACK, chat_id='C', user_id='U', scope_id='T')
    adapter = SimpleNamespace(_event_declares_bot_sender=lambda e: False, _resolve_user_is_bot=AsyncMock(return_value=False))
    await capture_learning(adapter, {'ts': '123.45'}, source, 'You forgot to ask about retention.')

@pytest.mark.asyncio
async def test_real_interview_ingress_learning_sees_no_quoted_history(tmp_path, monkeypatch):
    from tests.gateway.test_interview_ingress import setup_ingress, seed_interview, slack_event
    adapter, runner = setup_ingress(tmp_path)
    seed_interview(adapter, runner)
    captured = AsyncMock()
    monkeypatch.setattr('gateway.interview_learning.capture_learning', captured)
    event = slack_event('You forgot to ask about latency.', blocks=[{'type':'section','text':{
        'type':'mrkdwn','text':'Quoted: You forgot to ask about private billing.'}}])
    await adapter._handle_slack_message(event)
    assert captured.await_count == 1
    assert captured.await_args.args[3] == 'You forgot to ask about latency.'

@pytest.mark.asyncio
async def test_learning_requires_actual_event_author(tmp_path, monkeypatch):
    from gateway.interview_learning import capture_learning
    from gateway.config import Platform
    from gateway.session import SessionSource
    mocked = AsyncMock()
    monkeypatch.setattr('gateway.interview_learning.learn_message', mocked)
    monkeypatch.setattr('gateway.interview_learning.get_hermes_home', lambda: tmp_path / 'rex')
    source = SessionSource(platform=Platform.SLACK, chat_id='C', user_id='U', scope_id='T')
    adapter = SimpleNamespace(_event_declares_bot_sender=lambda e:False, _resolve_user_is_bot=AsyncMock(return_value=False))
    await capture_learning(adapter, {'ts':'2.0'}, source, 'Remember to ask about scope.')
    mocked.assert_not_awaited()
