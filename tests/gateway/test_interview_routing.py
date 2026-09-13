"""Interview controller behavior: real persisted state, fake UI/model boundaries."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from gateway.config import Platform
from gateway.session import SessionSource
from gateway.platforms.base import MessageEvent, SendResult


def event(text, user='U1', thread='th1', internal=False, id=None):
    import uuid
    return MessageEvent(text=text, message_id=id or uuid.uuid4().hex, source=SessionSource(platform=Platform.SLACK,
        chat_id='C1', user_id=user, thread_id=thread, scope_id='T1'), internal=internal)


def setup_controller(tmp_path, results):
    from gateway.interview import InterviewController
    from gateway.interview_store import InterviewStore
    ui = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True, message_id='summary1')),
        send_clarify=AsyncMock(return_value=SendResult(success=True, message_id='card1')))
    ui.send_interview_text = ui.send
    model = AsyncMock(side_effect=results)
    runner = SimpleNamespace(adapters={Platform.SLACK: ui}, _running_agents={},
        _is_user_authorized=lambda s: True, _session_key_for_source=lambda s: 'normal',
        _interview_turn=model)
    bank = {'version': 'v1', 'prompt': 'Ask useful questions.', 'questions': []}
    ctl = InterviewController(runner, InterviewStore(tmp_path/'interviews.sqlite3'), lambda: bank)
    return ctl, ui, model


@pytest.mark.asyncio
async def test_entry_question_is_persisted_and_normal_thread_unchanged(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='question', question='Who is it for?', choices=['A team','Myself'], messages=[])])
    assert await ctl.handle(event('/interview Build a dashboard')) == (True, None)
    rec = ctl.store.get(ctl.key(event('')))
    assert rec['phase'] == 'awaiting_answer'
    assert rec['pending']['message_id'] == 'card1'
    assert ui.send_clarify.await_args.kwargs['metadata']['thread_id'] == 'th1'
    assert await ctl.handle(event('normal chat', thread='other')) == (False, None)
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_summary_precedes_code_owned_plan_offer_and_explicit_plan_only(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='summary', text='Requirements summary.', messages=[]),
        dict(kind='plan', text='1. Agent A implements. 2. Agent B reviews.', messages=[])])
    order = []
    ui.send.side_effect = lambda **kw: order.append('summary') or SendResult(success=True, message_id='summary1')
    ui.send_clarify.side_effect = lambda **kw: order.append('question') or SendResult(success=True, message_id='card1')
    await ctl.handle(event('/interview Build a dashboard'))
    rec = ctl.store.get(ctl.key(event('')))
    assert order == ['summary', 'question']
    assert rec['phase'] == 'awaiting_plan_decision'
    assert rec['pending']['choices'] == ['Create the plan','Revise requirements','Stop here']
    await ctl.handle(event('looks good'))
    assert model.await_count == 1
    rec = ctl.store.get(rec['key'])
    assert rec['phase'] == 'awaiting_plan_decision'
    await ctl.accept(rec['key'], rec['pending']['nonce'], '0', 'U1', 'T1', 'C1', 'th1', 'card1')
    rec = ctl.store.get(rec['key'])
    assert rec['phase'] == 'plan_complete'
    assert model.await_args.kwargs['intent'] == 'plan'
    await ctl.handle(event('implement it now'))
    assert model.await_count == 2
    assert (await ctl.handle(event('/interview exit')))[0]
    assert await ctl.handle(event('new request')) == (False, None)


@pytest.mark.asyncio
async def test_other_answer_survives_reopen_and_owner_and_replay_checks(tmp_path):
    from gateway.interview import InterviewController
    from gateway.interview_store import InterviewStore
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='question', question='Audience?', choices=['Team','Public'], messages=[]),
        dict(kind='question', question='Deadline?', choices=[], messages=[])])
    await ctl.handle(event('/interview Dashboard'))
    rec = ctl.store.get(ctl.key(event('')))
    nonce = rec['pending']['nonce']
    # Fresh controller and connection emulate a gateway restart.
    ctl = InterviewController(ctl.runner, InterviewStore(tmp_path/'interviews.sqlite3'), ctl.bank_loader)
    args = (rec['key'], nonce, 'other', 'U1', 'T1', 'C1', 'th1', 'card1')
    await ctl.accept(*args)
    assert ctl.store.get(rec['key'])['pending']['awaiting_text'] is True
    await ctl.handle(event('Attack', user='U2'))
    assert model.await_count == 1
    await ctl.handle(event('Leadership'))
    assert model.await_count == 2
    assert ctl.store.get(rec['key'])['answers'][-1]['answer'] == 'Leadership'
    await ctl.accept(*args)
    assert model.await_count == 2
    await ctl.handle(event('/interview exit'))
    assert ctl.store.get(rec['key'])['pending'] is None


@pytest.mark.asyncio
async def test_finish_resume_and_stop_are_restricted_controls(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='question', question='Audience?', choices=[], messages=[]),
        dict(kind='summary', text='Summary', messages=[]), dict(kind='question', question='What to change?', choices=[], messages=[])])
    await ctl.handle(event('/interview Dashboard'))
    await ctl.handle(event('/interview finish'))
    assert model.await_args.kwargs['intent'] == 'summary'
    await ctl.handle(event('/interview resume'))
    assert ctl.store.get(ctl.key(event('')))['phase'] == 'awaiting_plan_decision'
    rec = ctl.store.get(ctl.key(event('')))
    await ctl.accept(rec['key'], rec['pending']['nonce'], '1', 'U1', 'T1', 'C1', 'th1', 'card1')
    assert ctl.store.get(ctl.key(event('')))['phase'] == 'awaiting_answer'
    await ctl.handle(event('/stop'))
    assert ctl.store.get(ctl.key(event('')))['phase'] == 'paused'
    await ctl.handle(event('/hrun do it'))
    assert model.await_count == 3


@pytest.mark.asyncio
async def test_explicit_read_root_is_pinned_and_missing_team_refused(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='question', question='Who?', choices=[], messages=[])])
    await ctl.handle(event(f'/interview --read-root "{tmp_path}" -- Dashboard'))
    rec = ctl.store.get(ctl.key(event('')))
    assert rec['task'] == 'Dashboard'
    assert rec['read_roots'] == [str(tmp_path.resolve())]
    assert model.await_args.args[0]['read_roots'] == rec['read_roots']


@pytest.mark.asyncio
async def test_model_failure_pauses_and_invalidates_no_routing_escape(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [RuntimeError('provider down')])
    with pytest.raises(RuntimeError):
        await ctl.handle(event('/interview Dashboard'))
    rec = ctl.store.get(ctl.key(event('')))
    assert rec['phase'] == 'paused'
    assert await ctl.handle(event('/hrun do it')) != (False, None)


@pytest.mark.asyncio
async def test_inflight_record_blocks_second_controller_and_exit(tmp_path):
    import asyncio
    from gateway.interview import InterviewController
    from gateway.interview_store import InterviewStore
    ctl, ui, model = setup_controller(tmp_path, [])
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return dict(kind='question', question='Who?', choices=[], messages=[])
    model.side_effect = slow
    task = asyncio.create_task(ctl.handle(event('/interview Dashboard')))
    await entered.wait()
    other = InterviewController(ctl.runner, InterviewStore(tmp_path/'interviews.sqlite3'), ctl.bank_loader)
    response = await other.handle(event('/interview exit'))
    assert 'progress' in response[1]
    release.set()
    await task
    assert ctl.store.get(ctl.key(event('')))['phase'] == 'awaiting_answer'


@pytest.mark.asyncio
async def test_typed_answer_replay_and_resume_reissues_saved_card(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [
        dict(kind='question', question='First?', choices=['A'], messages=[]),
        dict(kind='question', question='Second?', choices=['B'], messages=[])])
    await ctl.handle(event('/interview Dashboard'))
    answer = event('First answer', id='answer-1')
    await ctl.handle(answer)
    await ctl.handle(answer)
    assert model.await_count == 2
    rec = ctl.store.get(ctl.key(answer))
    nonce = rec['pending']['nonce']
    await ctl.handle(event('/interview resume'))
    current = ctl.store.get(ctl.key(answer))
    assert current['pending']['question'] == 'Second?'
    assert current['pending']['nonce'] != nonce
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_failed_summary_delivery_replays_saved_summary_before_offer(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='summary', text='Persisted summary', messages=[])])
    ui.send_interview_text.return_value = SendResult(success=False, error='offline')
    with pytest.raises(RuntimeError):
        await ctl.handle(event('/interview Dashboard'))
    assert model.await_count == 1
    ui.send_interview_text.return_value = SendResult(success=True, message_id='summary2')
    await ctl.handle(event('/interview resume'))
    assert model.await_count == 1
    assert ui.send_interview_text.call_args.kwargs['content'] == 'Persisted summary'
    rec = ctl.store.get(ctl.key(event('')))
    assert rec['phase'] == 'awaiting_plan_decision'
