"""Recoverable interview budgets: real runtime, scripted providers, no network."""
import asyncio
import copy
import json

import pytest

from agent import interview_runtime as runtime
from tests.agent.test_interview_runtime import Script, record, reply


def test_runtime_budgets_double_without_expanding_transcripts():
    assert runtime.MAX_ITERATIONS == 12
    assert runtime.TURN_TIMEOUT_SECONDS == 120.0
    assert runtime.COMPLETION_TIMEOUT_SECONDS == 60.0
    assert runtime.MAX_TOOL_CALLS == 32
    assert runtime.MAX_TRANSCRIPT_BYTES == 512_000


def test_exact_exhausted_research_resumes_without_replaying_completed_calls(monkeypatch):
    calls = []
    original_dispatch = runtime.dispatch_tool

    def research(name, arguments, **kwargs):
        if name in {'web_search', 'web_extract'}:
            calls.append((name, copy.deepcopy(arguments)))
            return {'content': f'research-{len(calls)}'}, None
        return original_dispatch(name, arguments, **kwargs)

    monkeypatch.setattr(runtime, 'dispatch_tool', research)
    rec = record()
    original = copy.deepcopy(rec)
    script = Script(*[reply(('web_search', {'query': f'topic {i}'}),
                            ('web_extract', {'urls': [f'https://example.org/{i}']}))
                      for i in range(12)])
    stopped = asyncio.run(runtime.run_interview_turn(rec, 'Research', completion=script))
    assert stopped['kind'] == 'budget'
    assert stopped['reason'] == 'iterations'
    assert len(script.requests) == 12
    assert len(calls) == 24
    assert stopped['diagnostics'][-2:] == [
        {'tool': 'web_search', 'status': 'success'},
        {'tool': 'web_extract', 'status': 'success'}]
    runtime._validate_transcript(stopped['messages'])
    results = [json.loads(m['content']) for m in stopped['messages'] if m['role'] == 'tool']
    assert results == [{'content': f'research-{i}'} for i in range(1, 25)]
    assert rec == original
    prefix = copy.deepcopy(stopped['messages'])
    rec['messages'] = stopped['messages']
    resumed = Script(reply(('clarify', {'question': 'Which audience?'})))
    result = asyncio.run(runtime.run_interview_turn(rec, 'Continue', completion=resumed))
    assert result['kind'] == 'question'
    assert result['messages'][:len(prefix)] == prefix
    assert resumed.requests[0]['messages'][:len(prefix)] == prefix
    assert len(calls) == 24
    runtime._validate_transcript(result['messages'])


@pytest.mark.parametrize('legacy', [False, True])
def test_budget_after_user_reminder_resumes_with_valid_unchanged_prefix(monkeypatch, legacy):
    monkeypatch.setattr(runtime, 'MAX_ITERATIONS', 1)
    rec = record()
    if legacy:
        rec['messages'] = [
            {'role': 'system', 'content': runtime.LEGACY_SYSTEM_PROMPT},
            {'role': 'user', 'content': 'Original task'},
            {'role': 'assistant', 'content': 'Working'},
            {'role': 'user', 'content': 'Use clarify to end this turn.'}]
    old_prefix = copy.deepcopy(rec['messages'])
    stopped = asyncio.run(runtime.run_interview_turn(
        rec, 'Start', completion=Script(reply(text='Still researching'))))
    assert stopped['kind'] == 'budget'
    runtime._validate_transcript(stopped['messages'])
    assert stopped['messages'][:len(old_prefix)] == old_prefix
    prefix = copy.deepcopy(stopped['messages'])
    followup = Script(reply(('interview_finish', {'text': 'Requirements'})))
    result = asyncio.run(runtime.run_interview_turn(
        dict(rec, messages=prefix), 'Continue', completion=followup))
    assert result['kind'] == 'summary'
    assert result['messages'][:len(prefix)] == prefix
    runtime._validate_transcript(followup.requests[0]['messages'])
    runtime._validate_transcript(result['messages'])


@pytest.mark.parametrize('budget', ['completion', 'total'])
def test_timeout_preserves_closed_research_checkpoint_and_resumes(monkeypatch, budget):
    monkeypatch.setattr(runtime, 'COMPLETION_TIMEOUT_SECONDS', 0.02 if budget == 'completion' else 1)
    monkeypatch.setattr(runtime, 'TURN_TIMEOUT_SECONDS', 0.02 if budget == 'total' else 1)
    requests = []

    async def completion(**kwargs):
        requests.append(copy.deepcopy(kwargs))
        if len(requests) == 1:
            return reply(('history_search', {'query': 'dashboard'}))
        await asyncio.sleep(10)

    monkeypatch.setattr(runtime, 'dispatch_tool', lambda *a, **kw: ({'content': 'Completed research'}, None))
    rec = record()
    original = copy.deepcopy(rec)
    result = asyncio.run(runtime.run_interview_turn(rec, 'Start', completion=completion))
    assert result['kind'] == 'budget'
    assert result['reason'] == 'timeout'
    assert result['messages'] == requests[-1]['messages']
    assert json.loads(result['messages'][-1]['content']) == {'content': 'Completed research'}
    runtime._validate_transcript(result['messages'])
    assert rec == original
    monkeypatch.undo()
    prefix = copy.deepcopy(result['messages'])
    resumed = asyncio.run(runtime.run_interview_turn(dict(rec, messages=prefix), 'Continue',
        completion=Script(reply(('clarify', {'question': 'Scope?'})))))
    assert resumed['kind'] == 'question'
    assert resumed['messages'][:len(prefix)] == prefix


@pytest.mark.parametrize('stop', ['total_timeout', 'cancel'])
def test_mid_batch_stop_closes_interrupted_and_skipped_calls_without_writes(monkeypatch, tmp_path, stop):
    import threading
    started = threading.Event()
    release = threading.Event()
    calls = []
    target = tmp_path / 'must-not-exist'
    monkeypatch.setattr(runtime, 'TURN_TIMEOUT_SECONDS', 0.05 if stop == 'total_timeout' else 10)
    original_dispatch = runtime.dispatch_tool

    def dispatch(name, args, **kwargs):
        calls.append(name)
        if len(calls) == 1:
            return {'content': 'Saved first result'}, None
        if len(calls) == 2:
            started.set()
            release.wait(2)
            raise RuntimeError('credential=must-never-leak')
        return original_dispatch(name, args, **kwargs)

    monkeypatch.setattr(runtime, 'dispatch_tool', dispatch)
    rec = record()
    original = copy.deepcopy(rec)

    async def scenario():
        task = asyncio.create_task(runtime.run_interview_turn(rec, 'Research', completion=Script(reply(
            ('web_search', {'query': 'first'}), ('web_extract', {'urls': ['https://example.org']}),
            ('terminal', {'command': f'touch {target}'})))))
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            if stop == 'cancel':
                task.cancel()
            result = await task
            snapshot = copy.deepcopy(result)
            release.set()
            await asyncio.sleep(0.02)
            assert result == snapshot  # late worker completion cannot overwrite checkpoint
            return result
        finally:
            release.set()

    result = asyncio.run(scenario())
    assert result['kind'] == 'budget'
    assert result['reason'] == 'timeout'
    runtime._validate_transcript(result['messages'])
    tools = [json.loads(m['content']) for m in result['messages'] if m['role'] == 'tool']
    assert tools[0] == {'content': 'Saved first result'}
    assert tools[1]['status'] == 'interrupted'
    assert tools[2]['status'] == 'skipped'
    assert all('error' in value for value in tools[1:])
    assert result['diagnostics'] == [
        {'tool': 'web_search', 'status': 'success'},
        {'tool': 'web_extract', 'status': 'interrupted'},
        {'tool': 'unknown', 'status': 'skipped'}]
    assert 'must-never-leak' not in json.dumps(result)
    assert calls == ['web_search', 'web_extract']
    assert not target.exists()
    assert rec == original
    monkeypatch.undo()
    prefix = copy.deepcopy(result['messages'])
    resumed = asyncio.run(runtime.run_interview_turn(dict(rec, messages=prefix), 'Continue',
        completion=Script(reply(('interview_finish', {'text': 'Summary'})))))
    assert resumed['kind'] == 'summary'
    assert calls == ['web_search', 'web_extract']
    assert resumed['messages'][:len(prefix)] == prefix


def test_diagnostics_are_bounded_and_distinguish_validation_denial_and_tool_failure(monkeypatch):
    monkeypatch.setattr(runtime, 'MAX_ITERATIONS', 2)
    secret = 'sk-do-not-leak-credentials-or-private-content'
    original_dispatch = runtime.dispatch_tool

    def dispatch(name, args, **kwargs):
        if name == 'web_search':
            return {'error': f'Transport failed: {secret}'}, None
        return original_dispatch(name, args, **kwargs)

    monkeypatch.setattr(runtime, 'dispatch_tool', dispatch)
    batch = reply(*[('web_search', {'query': secret}) for _ in range(32)])
    last = reply(('clarify', {'question': 7}), ('clarify', {'question': 'Q'}),
                 ('terminal', {'command': secret}), (secret, {}), ('web_search', {'query': secret}))
    last['choices'][0]['message']['tool_calls'][1]['function']['arguments'] = '{'
    result = asyncio.run(runtime.run_interview_turn(record(), 'Start', completion=Script(batch, last)))
    assert result['kind'] == 'budget'
    assert len(result['diagnostics']) == 32
    assert result['diagnostics'][-5:] == [
        {'tool': 'clarify', 'status': 'validation_error'},
        {'tool': 'clarify', 'status': 'validation_error'},
        {'tool': 'unknown', 'status': 'denied'},
        {'tool': 'unknown', 'status': 'denied'},
        {'tool': 'web_search', 'status': 'tool_error'}]
    assert all(set(item) == {'tool', 'status'} for item in result['diagnostics'])
    assert secret not in json.dumps(result['diagnostics'])


@pytest.mark.parametrize('error_type', [RuntimeError, ValueError, TimeoutError])
def test_dispatch_exception_is_closed_tool_error_not_budget_or_leaked(monkeypatch, error_type):
    original_dispatch = runtime.dispatch_tool

    def dispatch(name, args, **kwargs):
        if name == 'web_search':
            raise error_type('private credential=do-not-leak')
        return original_dispatch(name, args, **kwargs)

    monkeypatch.setattr(runtime, 'dispatch_tool', dispatch)
    result = asyncio.run(runtime.run_interview_turn(record(), 'Start', completion=Script(
        reply(('web_search', {'query': 'topic'}), ('clarify', {'question': 'Scope?'})))))
    assert result['kind'] == 'question'
    runtime._validate_transcript(result['messages'])
    tool_result = json.loads(next(m['content'] for m in result['messages'] if m['role'] == 'tool'))
    assert tool_result == {'error': 'Interview tool failed; execution remains disabled.'}
    assert 'do-not-leak' not in json.dumps(result)


def test_provider_failure_has_precise_safe_error_not_state_unavailable():
    async def broken(**kwargs):
        raise RuntimeError('credential=do-not-leak')

    with pytest.raises(RuntimeError, match='Interview completion failed; execution remains disabled') as exc:
        asyncio.run(runtime.run_interview_turn(record(), 'Start', completion=broken))
    assert 'do-not-leak' not in str(exc.value)


@pytest.mark.parametrize('reminder', [False, True])
def test_completion_timeout_checkpoint_matches_last_request_even_after_reminder(monkeypatch, reminder):
    monkeypatch.setattr(runtime, 'COMPLETION_TIMEOUT_SECONDS', 0.02)
    requests = []

    async def completion(**kwargs):
        requests.append(copy.deepcopy(kwargs['messages']))
        if reminder and len(requests) == 1:
            return reply(text='I will research')
        await asyncio.sleep(10)

    result = asyncio.run(runtime.run_interview_turn(record(), 'Start', completion=completion))
    assert result['kind'] == 'budget'
    assert result['messages'] == requests[-1]
    runtime._validate_transcript(result['messages'])
    resumed = Script(reply(('clarify', {'question': 'Scope?'})))
    end = asyncio.run(runtime.run_interview_turn(dict(record(), messages=result['messages']),
        'Continue', completion=resumed))
    assert end['kind'] == 'question'
    assert end['messages'][:len(result['messages'])] == result['messages']
    runtime._validate_transcript(end['messages'])
