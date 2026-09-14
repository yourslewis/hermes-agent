"""Permission UX: real durable state, mocked model/Slack transport only."""
import os
from pathlib import Path

import pytest

from tests.gateway.test_interview_routing import event, setup_controller

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Secure directory descriptors require POSIX')


def question():
    return dict(kind='question', question='Audience?', choices=['Team'], messages=[])


@pytest.mark.asyncio
@pytest.mark.parametrize('flag', ['--project', '--read-root'])
async def test_explicit_project_selection_grants_reads(tmp_path, flag):
    project = tmp_path / 'my project'
    project.mkdir()
    ctl, _, model = setup_controller(tmp_path, [question()])
    handled, response = await ctl.handle(event(f'!interview {flag} "{project}" -- Clarify migration'))
    assert handled and response is None
    rec = ctl.store.get(ctl.key(event('')))
    assert rec['task'] == 'Clarify migration'
    assert rec['read_roots'] == [str(project)]
    assert model.await_args.args[0]['read_roots'] == [str(project)]


@pytest.mark.asyncio
@pytest.mark.parametrize('target', ['/', 'home', 'profile', 'secret', 'symlink', 'ancestor_symlink', 'file', 'missing', 'broad', 'relative'])
async def test_unsafe_project_selection_never_creates_interview(tmp_path, monkeypatch, target):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    project = tmp_path / 'project'
    project.mkdir()
    (tmp_path / 'alias').symlink_to(project, target_is_directory=True)
    (project / 'child').mkdir()
    (tmp_path / 'file').write_text('not a directory')
    profile = home / '.hermes' / 'profiles' / 'work'
    profile.mkdir(parents=True)
    secret = project / 'secrets'
    secret.mkdir()
    paths = {'/': '/', 'home': str(home), 'profile': str(profile), 'secret': str(secret),
             'symlink': str(tmp_path / 'alias'), 'ancestor_symlink': str(tmp_path / 'alias' / 'child'),
             'file': str(tmp_path / 'file'), 'missing': str(tmp_path / 'missing'),
             'broad': '/Users', 'relative': 'project'}
    ctl, _, model = setup_controller(tmp_path, [question()])
    handled, response = await ctl.handle(event(f'!interview --project "{paths[target]}" -- Clarify'))
    assert handled and response
    assert ctl.store.get(ctl.key(event(''))) is None
    assert model.await_count == 0


@pytest.mark.asyncio
async def test_approve_read_preserves_existing_interview_and_does_not_infer_paths(tmp_path):
    project = tmp_path / 'project'
    project.mkdir()
    ctl, _, model = setup_controller(tmp_path, [question()])
    await ctl.handle(event(f'!interview Clarify {project}'))
    rec = ctl.store.get(ctl.key(event('')))
    rec = ctl.store.update(rec['key'], rec['revision'],
        answers=[{'question': 'Prior?', 'answer': 'Yes'}], messages=[{'role': 'user', 'content': 'unchanged'}])
    assert rec['read_roots'] == []
    handled, response = await ctl.handle(event(f'!interview approve-read "{project}"'))
    assert handled and 'approved' in response.lower()
    after = ctl.store.get(rec['key'])
    assert after['read_roots'] == [str(project)]
    for field in ('id', 'task', 'bank', 'messages', 'answers', 'pending', 'phase'):
        assert after[field] == rec[field]
    assert model.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('allowed', [True, False])
async def test_history_approval_uses_validator_and_preserves_prefix(tmp_path, monkeypatch, allowed):
    import sys
    from types import ModuleType
    ctl, _, model = setup_controller(tmp_path, [question()])
    await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    calls = []
    helper = ModuleType('agent.interview_history')
    def validate(record, session_id):
        calls.append((record, session_id))
        if not allowed:
            raise ValueError('Session unavailable in this owner/profile/source scope')
        return session_id
    helper.validate_history_session = validate
    monkeypatch.setitem(sys.modules, 'agent.interview_history', helper)
    _, response = await ctl.handle(event('!interview approve-history session-123'))
    assert calls == [(before, 'session-123')]
    after = ctl.store.get(before['key'])
    assert after.get('history_sessions', []) == (['session-123'] if allowed else [])
    assert ('approved' if allowed else 'unavailable') in response.lower()
    for field in ('id', 'bank', 'messages', 'answers', 'pending', 'phase'):
        assert after[field] == before[field]
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_status_lists_research_capabilities_and_grants(tmp_path):
    ctl, _, model = setup_controller(tmp_path, [question()])
    await ctl.handle(event('!interview Clarify'))
    rec = ctl.store.get(ctl.key(event('')))
    ctl.store.update(rec['key'], rec['revision'], read_roots=[str(tmp_path)], history_sessions=['session-123'])
    _, status = await ctl.handle(event('!interview status'))
    assert str(tmp_path) in status and 'session-123' in status
    assert 'web' in status.lower() and 'current interview' in status.lower()
    assert 'execution is disabled' in status.lower()
    assert model.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('token', ['0', '1'])
async def test_permission_card_is_durable_before_send_and_accepts_after_restart(tmp_path, token):
    from gateway.interview import InterviewController
    from gateway.interview_store import InterviewStore
    from gateway.platforms.base import SendResult
    project = tmp_path / 'project'
    project.mkdir()
    messages = [{'role': 'assistant', 'content': 'pending read request'}]
    ctl, ui, model = setup_controller(tmp_path, [
        dict(kind='permission', path=str(project), reason='Inspect package metadata', messages=messages), question()])
    def delivered(**kwargs):
        saved = InterviewStore(tmp_path / 'interviews.sqlite3').get(ctl.key(event('')))
        assert saved['pending']['kind'] == 'read_permission'
        assert saved['pending']['path'] == str(project)
        assert saved['read_roots'] == []
        assert saved['pending']['message_id'] == ''
        assert 'U1' in kwargs['question'] and str(project) in kwargs['question']
        assert 'Inspect package metadata' in kwargs['question']
        return SendResult(success=True, message_id='permission-card')
    ui.send_clarify.side_effect = delivered
    await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    assert before['messages'] == messages
    assert before['pending']['choices'] == ['Approve read access', 'Deny read access']
    ctl = InterviewController(ctl.runner, InterviewStore(tmp_path / 'interviews.sqlite3'), ctl.bank_loader)
    ui.send_clarify.side_effect = None
    outcome = await ctl.accept(before['key'], before['pending']['nonce'], token,
        'U1', 'T1', 'C1', 'th1', 'permission-card')
    after = ctl.store.get(before['key'])
    assert after['read_roots'] == ([str(project)] if token == '0' else [])
    assert after['answers'] == before['answers']
    assert ('approved' if token == '0' else 'denied') in outcome.lower()
    assert model.await_count == 2
    assert model.await_args.args[0]['messages'] == messages
    assert model.await_args.args[0]['read_roots'] == after['read_roots']
    await ctl.accept(before['key'], before['pending']['nonce'], token, 'U1', 'T1', 'C1', 'th1', 'permission-card')
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_permission_resume_rotates_nonce_keeps_path_and_never_accepts_text(tmp_path):
    ctl, _, model = setup_controller(tmp_path, [
        dict(kind='permission', path=str(tmp_path), reason='Read design', messages=[]), question()])
    await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    await ctl.handle(event('Yes, approve read access'))
    assert ctl.store.get(before['key']) == before
    await ctl.accept(before['key'], before['pending']['nonce'], 'other', 'U1', 'T1', 'C1', 'th1', 'card1')
    assert ctl.store.get(before['key']) == before
    await ctl.handle(event('!interview resume'))
    after = ctl.store.get(before['key'])
    assert after['pending']['path'] == str(tmp_path)
    assert after['pending']['nonce'] != before['pending']['nonce']
    assert after['read_roots'] == [] and after['answers'] == []
    assert model.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['owner', 'team', 'channel', 'thread', 'message', 'nonce', 'lease', 'undelivered', 'symlink', 'token'])
async def test_permission_click_rejects_invalid_identity_or_changed_root(tmp_path, bad):
    import time
    project = tmp_path / 'project'
    project.mkdir()
    ctl, _, model = setup_controller(tmp_path, [
        dict(kind='permission', path=str(project), reason='Read design', messages=[]), question()])
    await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    args = dict(key=before['key'], nonce=before['pending']['nonce'], token='0', owner='U1',
                team='T1', channel='C1', thread='th1', message_id='card1')
    if bad in {'owner', 'team', 'channel', 'thread', 'nonce', 'token'}:
        args[bad] = 'wrong'
    elif bad == 'message':
        args['message_id'] = 'wrong'
    elif bad == 'lease':
        before = ctl.store.update(before['key'], before['revision'], busy_until=time.time() + 100)
    elif bad == 'undelivered':
        pending = dict(before['pending'], message_id='')
        before = ctl.store.update(before['key'], before['revision'], pending=pending)
        args['message_id'] = ''
    elif bad == 'symlink':
        project.rmdir()
        project.symlink_to(tmp_path, target_is_directory=True)
    await ctl.accept(**args)
    assert ctl.store.get(before['key']) == before
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_arbitrary_model_question_cannot_authorize_reads(tmp_path):
    ctl, _, model = setup_controller(tmp_path, [
        dict(kind='question', question=f'Approve {tmp_path}?', choices=['Approve read access'],
             path=str(tmp_path), messages=[]), question()])
    await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    await ctl.accept(before['key'], before['pending']['nonce'], '0', 'U1', 'T1', 'C1', 'th1', 'card1')
    assert ctl.store.get(before['key'])['read_roots'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('control', ['approve-read /', 'approve-history session-123', 'status'])
async def test_owner_guard_precedes_control_and_permission_validation(tmp_path, control):
    ctl, _, _ = setup_controller(tmp_path, [question()])
    await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    ctl.runner._check_slash_access = lambda *args: pytest.fail('Control access evaluated before owner guard')
    _, response = await ctl.handle(event(f'!interview {control}', user='U2'))
    assert 'owner' in response.lower()
    assert ctl.store.get(before['key']) == before


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix', ['approve-read /', 'approve-history abc', '--project "unterminated -- Task', '--project /', '--unknown / -- Task'])
async def test_malformed_or_control_entry_never_creates_interview(tmp_path, suffix):
    ctl, _, model = setup_controller(tmp_path, [question()])
    handled, response = await ctl.handle(event('!interview ' + suffix))
    assert handled and response
    assert ctl.store.get(ctl.key(event(''))) is None
    assert model.await_count == 0


@pytest.mark.asyncio
async def test_busy_adapter_refuses_bang_interview_entry(tmp_path):
    from types import SimpleNamespace
    from gateway.interview import adapter_interview_admission
    ctl, ui, model = setup_controller(tmp_path, [])
    ctl.runner._interview_controller = ctl
    ui._message_handler = SimpleNamespace(__self__=ctl.runner)
    ui.config = SimpleNamespace(extra={})
    from gateway.session import build_session_key
    ev = event('!interview Clarify')
    ui._active_sessions = {build_session_key(ev.source)}
    assert await adapter_interview_admission(ui, ev)
    assert 'busy' in ui.send.await_args.kwargs['content']
    assert model.await_count == 0


@pytest.mark.asyncio
async def test_permission_failed_delivery_replays_durable_path(tmp_path):
    from gateway.platforms.base import SendResult
    ctl, ui, model = setup_controller(tmp_path, [
        dict(kind='permission', path=str(tmp_path), reason='Read design', messages=[])])
    ui.send_clarify.return_value = SendResult(success=False, error='offline')
    with pytest.raises(RuntimeError):
        await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    assert before['phase'] == 'paused' and before['pending']['path'] == str(tmp_path)
    assert before['read_roots'] == []
    ui.send_clarify.return_value = SendResult(success=True, message_id='retry')
    await ctl.handle(event('!interview resume'))
    after = ctl.store.get(before['key'])
    assert after['pending']['path'] == str(tmp_path) and after['pending']['message_id'] == 'retry'
    assert after['pending']['nonce'] != before['pending']['nonce']
    assert model.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    {'path': '/', 'reason': 'Read all'},
    {'path': 'project-name', 'reason': 'Read design'},
    {'path': None, 'reason': 'Read design'},
    {'reason': 'Missing path'},
    {'path': 'VALID', 'reason': None},
    {'path': 'VALID', 'reason': ''},
    {'path': 'VALID', 'reason': 'x' * 32_769},
])
async def test_invalid_model_permission_cannot_offer_approval(tmp_path, payload):
    payload = dict(payload)
    if payload.get('path') == 'VALID':
        payload['path'] = str(tmp_path)
    messages = [{'role': 'assistant', 'content': 'Saved research'}]
    ctl, ui, _ = setup_controller(tmp_path, [dict(kind='permission', messages=messages, **payload)])
    assert await ctl.handle(event('!interview Clarify')) == (True, None)
    after = ctl.store.get(ctl.key(event('')))
    assert after['read_roots'] == [] and after['pending'] is None
    assert after['phase'] == 'paused' and ui.send_clarify.await_count == 0
    assert after['messages'] == messages and after['busy_until'] == 0
    assert after['plan'] == '' and after['summary'] == ''
    notice = ui.send_interview_text.await_args.kwargs['content']
    assert 'permission' in notice.lower() and '!interview approve-read' in notice
    assert '/interview' not in notice


def test_project_checkouts_allowed_but_profile_checkouts_rejected(tmp_path, monkeypatch):
    from gateway.interview_permissions import validate_read_root
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('HERMES_HOME', str(home / '.hermes'))
    project = home / '.hermes' / 'worktrees' / 'app'
    project.mkdir(parents=True)
    assert validate_read_root(str(project)) == str(project)
    profile = home / '.hermes' / 'profiles' / 'work' / 'repos' / 'app'
    profile.mkdir(parents=True)
    with pytest.raises(ValueError):
        validate_read_root(str(profile))


def test_command_registry_lists_permission_controls():
    from hermes_cli.commands import COMMAND_REGISTRY
    command = next(c for c in COMMAND_REGISTRY if c.name == 'interview')
    assert {'approve-read', 'approve-history'} <= set(command.subcommands)
    assert '--project' in command.args_hint


@pytest.mark.parametrize('path', ['//', '//Users', '/usr/local/share', '/etc/ssl', '/System/Library', '/private/var/db'])
def test_nonproject_system_roots_fail_closed(path):
    from gateway.interview_permissions import validate_read_root
    with pytest.raises(ValueError):
        validate_read_root(path)


@pytest.mark.asyncio
async def test_approve_read_command_resolves_matching_permission_without_restarting(tmp_path):
    ctl, _, model = setup_controller(tmp_path, [
        dict(kind='permission', path=str(tmp_path), reason='Read design', messages=[]), question()])
    await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    await ctl.handle(event(f'!interview approve-read "{tmp_path}"'))
    after = ctl.store.get(before['key'])
    assert after['pending'] is None and after['read_roots'] == [str(tmp_path)]
    for field in ('id', 'task', 'bank', 'messages', 'answers'):
        assert after[field] == before[field]
    assert model.await_count == 1
    await ctl.handle(event('!interview resume'))
    assert model.await_count == 2
    assert ctl.store.get(before['key'])['pending']['kind'] == 'answer'


@pytest.mark.asyncio
async def test_real_runtime_requests_then_reads_only_after_owner_approval(tmp_path):
    from functools import partial
    from agent.interview_runtime import run_interview_turn
    from tests.agent.test_interview_runtime import Script, reply
    project = tmp_path / 'project'
    project.mkdir()
    (project / 'README.md').write_text('Unique approved project context')
    ctl, _, _ = setup_controller(tmp_path, [])
    script = Script(
        reply(('request_read_access', {'path': str(project), 'reason': 'Read README'})),
        reply(('read_file', {'path': str(project / 'README.md')})),
        reply(('clarify', {'question': 'Which audience?', 'choices': ['Team']})))
    ctl.runner._interview_turn = partial(run_interview_turn, completion=script)
    await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    assert before['pending']['kind'] == 'read_permission' and before['read_roots'] == []
    await ctl.accept(before['key'], before['pending']['nonce'], '0', 'U1', 'T1', 'C1', 'th1', 'card1')
    after = ctl.store.get(before['key'])
    assert after['messages'][:len(before['messages'])] == before['messages']
    assert any('Unique approved project context' in m.get('content', '') for m in after['messages'] if m['role'] == 'tool')
    assert after['pending']['kind'] == 'answer'


@pytest.mark.asyncio
@pytest.mark.parametrize('guard', ['lease', 'lock', 'internal'])
@pytest.mark.parametrize('control', ['approve-read', 'approve-history'])
async def test_permission_controls_obey_existing_admission_guards(tmp_path, guard, control):
    import asyncio
    import time
    ctl, _, model = setup_controller(tmp_path, [question()])
    await ctl.handle(event('!interview Clarify'))
    rec = ctl.store.get(ctl.key(event('')))
    if guard == 'lease':
        rec = ctl.store.update(rec['key'], rec['revision'], busy_until=time.time() + 100)
    lock = ctl.locks.setdefault(rec['key'], asyncio.Lock())
    if guard == 'lock':
        await lock.acquire()
    try:
        await ctl.handle(event(f'!interview {control} "{tmp_path}"', internal=guard == 'internal'))
        assert ctl.store.get(rec['key']) == rec
        assert model.await_count == 1
    finally:
        if lock.locked():
            lock.release()


@pytest.mark.asyncio
@pytest.mark.parametrize('containers', [('repos',), ('repos', 'worktrees')])
async def test_project_name_requires_path_choice_then_explicit_permission(tmp_path, monkeypatch, containers):
    from gateway.interview import InterviewController
    from gateway.interview_store import InterviewStore
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    candidates = []
    for container in containers:
        project = home / '.hermes' / container / 'sample-app'
        project.mkdir(parents=True)
        candidates.append(str(project))
    candidates.sort()
    ctl, _, model = setup_controller(tmp_path, [question(), question()])
    await ctl.handle(event('!interview Clarify'))
    before = ctl.store.get(ctl.key(event('')))
    assert await ctl.handle(event('!interview approve-read sample-app')) == (True, None)
    choice = ctl.store.get(before['key'])
    assert choice['pending']['kind'] == 'project_choice'
    assert choice['pending']['choices'] == candidates
    assert choice['read_roots'] == []
    for field in ('id', 'task', 'bank', 'messages', 'answers'):
        assert choice[field] == before[field]
    assert model.await_count == 1
    await ctl.handle(event('approve it'))
    await ctl.accept(choice['key'], choice['pending']['nonce'], 'other', 'U1', 'T1', 'C1', 'th1', 'card1')
    assert ctl.store.get(choice['key']) == choice
    ctl = InterviewController(ctl.runner, InterviewStore(tmp_path / 'interviews.sqlite3'), ctl.bank_loader)
    await ctl.handle(event('!interview resume'))
    replay = ctl.store.get(choice['key'])
    assert replay['pending']['choices'] == candidates
    assert replay['pending']['nonce'] != choice['pending']['nonce']
    await ctl.accept(choice['key'], choice['pending']['nonce'], '0', 'U1', 'T1', 'C1', 'th1', 'card1')
    assert ctl.store.get(choice['key']) == replay
    await ctl.accept(replay['key'], replay['pending']['nonce'], '0', 'U1', 'T1', 'C1', 'th1', 'card1')
    permission = ctl.store.get(choice['key'])
    assert permission['pending']['kind'] == 'read_permission'
    assert permission['pending']['path'] == candidates[0]
    assert permission['read_roots'] == [] and model.await_count == 1
    await ctl.accept(permission['key'], permission['pending']['nonce'], '0', 'U1', 'T1', 'C1', 'th1', 'card1')
    assert ctl.store.get(choice['key'])['read_roots'] == [candidates[0]]
    assert model.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('entry', [True, False])
async def test_tilde_project_path_is_expanded_and_validated_without_symlink_resolution(tmp_path, monkeypatch, entry):
    home = tmp_path / 'home'
    project = home / 'project'
    project.mkdir(parents=True)
    monkeypatch.setenv('HOME', str(home))
    ctl, _, model = setup_controller(tmp_path, [question()])
    if entry:
        response = await ctl.handle(event('!interview --project ~/project -- Clarify'))
    else:
        await ctl.handle(event('!interview Clarify'))
        response = await ctl.handle(event('!interview approve-read ~/project'))
    rec = ctl.store.get(ctl.key(event('')))
    assert rec is not None, response
    assert rec['read_roots'] == [str(project)]
    assert model.await_count == 1
    project.rmdir()
    project.symlink_to(tmp_path, target_is_directory=True)
    before = ctl.store.get(rec['key'])
    _, response = await ctl.handle(event('!interview approve-read ~/project'))
    assert 'denied' in response.lower()
    assert ctl.store.get(rec['key']) == before


@pytest.mark.asyncio
async def test_bare_project_entry_is_actionable_and_does_not_guess(tmp_path):
    ctl, _, model = setup_controller(tmp_path, [])
    _, response = await ctl.handle(event('!interview --project sample-app -- Clarify'))
    assert '!interview approve-read' in response and '!interview <task>' in response
    assert ctl.store.get(ctl.key(event(''))) is None and model.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['no_thread', 'complete', 'paused', 'stop', 'disabled_command', 'other'])
async def test_controller_user_facing_guidance_uses_bang_syntax(tmp_path, scenario):
    ctl, _, _ = setup_controller(tmp_path, [question()])
    if scenario == 'no_thread':
        _, response = await ctl.handle(event('!interview', thread=''))
    else:
        await ctl.handle(event('!interview Clarify'))
        rec = ctl.store.get(ctl.key(event('')))
        if scenario == 'other':
            response = await ctl.accept(rec['key'], rec['pending']['nonce'], 'other',
                                        'U1', 'T1', 'C1', 'th1', 'card1')
        else:
            if scenario in {'complete', 'paused'}:
                ctl.store.update(rec['key'], rec['revision'], phase=scenario, pending=None)
            text = {'stop': '/stop', 'disabled_command': '/hrun task'}.get(scenario, 'run it')
            _, response = await ctl.handle(event(text))
    assert '!interview' in response and '/interview' not in response
