"""Regression for grant -> exhausted resume -> durable continue, no real model/network."""
import json
import pytest
from tests.gateway.test_interview_routing import event, setup_controller
from agent.interview_runtime import run_interview_turn


def reply(name, args):
    return {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
        {'id': 'call', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(args)}}]}}]}


@pytest.mark.asyncio
async def test_approved_resume_budget_retains_work_and_recovers_after_reopen(tmp_path, monkeypatch):
    import agent.interview_runtime as rt
    from gateway.interview_store import InterviewStore
    monkeypatch.setattr(rt, 'MAX_ITERATIONS', 2)
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'schema.txt').write_text('event_id, user_id, timestamp')
    ctl, ui, _ = setup_controller(tmp_path, [])
    initial = True
    finish = False
    calls = []
    async def completion(**kw):
        nonlocal initial
        if initial:
            initial = False
            return reply('clarify', {'question': 'Audience?', 'choices': ['Team']})
        if finish:
            assert any(m.get('role') == 'tool' and 'event_id' in m.get('content', '') for m in kw['messages'])
            return reply('clarify', {'question': 'Latency?', 'choices': ['Seconds', 'Minutes']})
        calls.append('read')
        return reply('read_file', {'path': str(project / 'schema.txt')})
    async def turn(record, text, intent='collect'):
        return await run_interview_turn(record, text, intent, completion)
    ctl.runner._interview_turn = turn
    await ctl.handle(event('!interview Clarify processing'))
    key = ctl.key(event(''))
    before = ctl.store.get(key)
    answers = [{'question': 'Prior?', 'answer': str(i)} for i in range(11)]
    ctl.store.update(key, before['revision'], phase='paused', pending=None, answers=answers)
    await ctl.handle(event(f'!interview approve-read {project}'))
    await ctl.handle(event('!interview resume'))
    rec = ctl.store.get(key)
    assert rec['pending']['kind'] == 'budget'
    assert rec['answers'] == answers
    assert rec['read_roots'] == [str(project)]
    assert len(calls) == 2
    saved = rec['messages']
    ctl.store = InterviewStore(tmp_path / 'interviews.sqlite3')
    await ctl.handle(event('!interview resume'))
    rec = ctl.store.get(key)
    assert len(calls) == 2  # reissue, not rerun the exhausted turn
    finish = True
    await ctl.accept(key, rec['pending']['nonce'], '0', 'U1', 'T1', 'C1', 'th1', 'card1')
    rec = ctl.store.get(key)
    assert rec['messages'][:len(saved)] == saved
    assert rec['pending']['question'] == 'Latency?'
    assert rec['answers'][:11] == answers


@pytest.mark.asyncio
async def test_relative_project_permission_then_approval_then_read(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    project = home / '.hermes' / 'repos' / 'msan_algo'
    project.mkdir(parents=True)
    (project / 'schema.txt').write_text('event_id')
    ctl, ui, _ = setup_controller(tmp_path, [])
    calls = iter([
        reply('request_read_access', {'path': '.hermes/repos/msan_algo', 'reason': 'Read schema'}),
        reply('read_file', {'path': '.hermes/repos/msan_algo/schema.txt'}),
        reply('clarify', {'question': 'Latency?', 'choices': ['Seconds']})])
    async def completion(**kw):
        return next(calls)
    async def turn(record, text, intent='collect'):
        return await run_interview_turn(record, text, intent, completion)
    ctl.runner._interview_turn = turn
    await ctl.handle(event('!interview Clarify processing'))
    key = ctl.key(event(''))
    rec = ctl.store.get(key)
    assert rec['pending']['kind'] == 'read_permission'
    assert rec['pending']['path'] == str(project)
    assert rec['read_roots'] == []
    await ctl.accept(key, rec['pending']['nonce'], '0', 'U1', 'T1', 'C1', 'th1', 'card1')
    rec = ctl.store.get(key)
    assert rec['read_roots'] == [str(project)]
    assert rec['pending']['question'] == 'Latency?'
    assert any(m.get('role') == 'tool' and 'event_id' in m.get('content', '') for m in rec['messages'])
