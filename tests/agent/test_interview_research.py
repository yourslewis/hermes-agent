"""Research capabilities preserve the interview's execution boundary."""
import json
import pytest
from agent.interview_policy import dispatch_tool, tool_schemas
from agent.interview_runtime import run_interview_turn


def test_permission_request_does_not_grant_reads(tmp_path):
    project = str(tmp_path.resolve())
    record = {'read_roots': []}
    result, outcome = dispatch_tool('request_read_access', {'path': project, 'reason': 'Inspect schema'})
    assert outcome and outcome['kind'] == 'permission'
    assert outcome['path'] == project
    assert record['read_roots'] == []
    assert result['status'] == 'pending'


@pytest.mark.asyncio
async def test_existing_interview_gets_current_capabilities_without_prefix_rewrite(tmp_path):
    from agent.interview_runtime import SYSTEM_PROMPT
    prefix = [{'role': 'system', 'content': SYSTEM_PROMPT},
              {'role': 'user', 'content': 'Old task'},
              {'role': 'assistant', 'content': 'Old question'}]
    record = {'messages': prefix, 'read_roots': ['/tmp/project'], 'history_sessions': ['s1']}
    seen = []
    async def completion(**kw):
        seen.append(kw)
        return {'choices': [{'message': {'role': 'assistant', 'content': None, 'tool_calls': [
            {'id': 'p1', 'type': 'function', 'function': {'name': 'request_read_access',
             'arguments': json.dumps({'path': str(tmp_path.resolve()), 'reason': 'Need schema'})}}]}}]}
    result = await run_interview_turn(record, 'Continue', completion=completion)
    assert result['kind'] == 'permission'
    assert result['messages'][:len(prefix)] == prefix
    turn = seen[0]['messages'][-1]['content']
    assert 'web_search' in turn and '/tmp/project' in turn and 's1' in turn
    assert record['messages'] == prefix


def test_research_tools_are_explicit_and_dispatch_has_no_execution(monkeypatch):
    import sys
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, 'agent.interview_web', SimpleNamespace(
        web_search=lambda query, limit=5: {'query': query},
        web_extract=lambda urls: {'urls': urls}))
    monkeypatch.setitem(sys.modules, 'agent.interview_history', SimpleNamespace(
        history_search=lambda record, **kw: {'scope': record['history_sessions']}))
    record = {'history_sessions': ['approved']}
    for name, args, expected in [
        ('web_search', {'query': 'public architecture'}, {'query': 'public architecture'}),
        ('web_extract', {'urls': ['https://example.com']}, {'urls': ['https://example.com']}),
        ('history_search', {'query': 'decisions'}, {'scope': ['approved']})]:
        result, outcome = dispatch_tool(name, args, record=record)
        assert result == expected and outcome is None
    assert 'error' in dispatch_tool('terminal', {'command': 'pwd'}, record=record)[0]
    assert 'error' in dispatch_tool('history_search', {'profile': 'other'}, record=record)[0]


def test_selected_project_supports_relative_file_paths(tmp_path):
    root = tmp_path.resolve()
    (root / 'schema.txt').write_text('event_id, timestamp')
    result, _ = dispatch_tool('read_file', {'path': 'schema.txt'}, read_roots=[str(root)])
    assert result['content'] == 'event_id, timestamp'
    denied, _ = dispatch_tool('read_file', {'path': '../outside'}, read_roots=[str(root)])
    assert 'error' in denied


@pytest.mark.asyncio
async def test_legacy_interview_receives_authoritative_append_only_policy_upgrade():
    from agent.interview_runtime import LEGACY_SYSTEM_PROMPT, SYSTEM_PROMPT
    prefix = [{'role': 'system', 'content': LEGACY_SYSTEM_PROMPT},
              {'role': 'user', 'content': 'Old task'},
              {'role': 'assistant', 'content': 'Question'}]
    seen = []
    async def completion(**kw):
        seen.append(kw['messages'])
        return {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': 'q', 'type': 'function', 'function': {'name': 'clarify',
             'arguments': json.dumps({'question': 'Next?', 'choices': []})}}]}}]}
    result = await run_interview_turn({'messages': prefix}, 'Continue', completion=completion)
    assert result['messages'][:3] == prefix
    assert {'role': 'system', 'content': SYSTEM_PROMPT} in seen[0][3:]


@pytest.mark.asyncio
async def test_runtime_passes_trusted_history_scope_and_stops_after_permission(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace
    seen = []
    monkeypatch.setitem(sys.modules, 'agent.interview_history', SimpleNamespace(
        history_search=lambda record, **kw: seen.append(record) or {'messages': []}))
    count = 0
    async def completion(**kw):
        nonlocal count
        count += 1
        calls = [('history_search', {})] if count == 1 else [
            ('request_read_access', {'path': str(tmp_path.resolve()), 'reason': 'schema'}),
            ('history_search', {})]
        return {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': str(count)+str(i), 'type': 'function', 'function': {'name': name,
             'arguments': json.dumps(args)}} for i, (name, args) in enumerate(calls)]}}]}
    record = {'source': {'team': 'T1'}, 'owner': 'U1', 'history_sessions': ['s1']}
    result = await run_interview_turn(record, 'Read earlier decisions', completion=completion)
    assert len(seen) == 1 and seen[0]['owner'] == 'U1'
    assert seen[0]['history_sessions'] == ['s1']
    assert result['kind'] == 'permission'
    assert 'Denied' in result['messages'][-1]['content']


@pytest.mark.asyncio
async def test_current_grants_are_trusted_system_metadata():
    async def completion(**kw):
        metadata = [m for m in kw['messages'] if m['role'] == 'system' and m['content'].startswith('Interview access grants: ')]
        assert metadata and '/approved/project' in metadata[-1]['content']
        return {'choices': [{'message': {'role': 'assistant', 'tool_calls': [
            {'id': 'q', 'type': 'function', 'function': {'name': 'clarify', 'arguments': '{"question":"Next?"}'}}]}}]}
    await run_interview_turn({'read_roots': ['/approved/project']}, 'Continue', completion=completion)
