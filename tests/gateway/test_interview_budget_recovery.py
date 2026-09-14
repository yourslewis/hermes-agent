"""Recovery cards use durable checkpoints, never implicit planning consent."""
import pytest

from gateway.interview import InterviewController
from gateway.interview_store import InterviewStore
from gateway.platforms.base import SendResult
from tests.gateway.test_interview_routing import event, setup_controller

CHOICES = ['Continue research', 'Summarize what is known', 'Stop here']
MESSAGES = [{'role': 'user', 'content': 'Research this'},
            {'role': 'assistant', 'content': 'Partial findings'}]
DIAGNOSTICS = [{'name': 'web_search', 'status': 'completed'}]


def budget(reason='iterations'):
    return dict(kind='budget', reason=reason, messages=MESSAGES, diagnostics=DIAGNOSTICS)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['iterations', 'timeout'])
async def test_budget_checkpoint_and_code_owned_card_persist_before_delivery(tmp_path, reason):
    ctl, ui, model = setup_controller(tmp_path, [budget(reason)])
    def delivered(**kwargs):
        saved = InterviewStore(tmp_path / 'interviews.sqlite3').get(ctl.key(event('')))
        assert saved['messages'] == MESSAGES
        assert saved['budget'] == {'reason': reason, 'diagnostics': DIAGNOSTICS}
        assert saved['pending']['kind'] == 'budget'
        assert saved['pending']['choices'] == CHOICES
        assert saved['pending']['message_id'] == ''
        assert 'execution' in kwargs['question'].lower()
        return SendResult(success=True, message_id='card1')
    ui.send_clarify.side_effect = delivered
    assert await ctl.handle(event('!interview Research this')) == (True, None)
    saved = ctl.store.get(ctl.key(event('')))
    assert saved['pending']['message_id'] == 'card1'
    assert saved['busy_until'] == 0
    assert saved['plan'] == '' and saved['summary'] == ''
    assert model.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('token,intent', [('0', 'collect'), ('1', 'summary'), ('2', None)])
async def test_budget_actions_after_restart_keep_checkpoint_and_never_plan(tmp_path, token, intent):
    ctl, _, model = setup_controller(tmp_path, [budget(),
        dict(kind='summary', text='Known and unresolved', messages=MESSAGES)])
    await ctl.handle(event('!interview Research this'))
    before = ctl.store.get(ctl.key(event('')))
    ctl = InterviewController(ctl.runner, InterviewStore(tmp_path / 'interviews.sqlite3'), ctl.bank_loader)
    args = (before['key'], before['pending']['nonce'], token, 'U1', 'T1', 'C1', 'th1', 'card1')
    await ctl.accept(*args)
    after = ctl.store.get(before['key'])
    assert after['plan'] == ''
    if intent:
        assert model.await_count == 2
        assert model.await_args.kwargs['intent'] == intent
        assert model.await_args.args[0]['messages'] == MESSAGES
    else:
        assert model.await_count == 1
        assert after['phase'] in {'paused', 'complete'} and after['pending'] is None
        assert await ctl.handle(event('execute now')) != (False, None)
    await ctl.accept(*args)
    assert model.await_count == (2 if intent else 1)


@pytest.mark.asyncio
async def test_budget_other_and_text_do_not_consume_card_resume_rotates_nonce(tmp_path):
    ctl, _, model = setup_controller(tmp_path, [budget()])
    await ctl.handle(event('!interview Research this'))
    before = ctl.store.get(ctl.key(event('')))
    await ctl.accept(before['key'], before['pending']['nonce'], 'other', 'U1', 'T1', 'C1', 'th1', 'card1')
    assert ctl.store.get(before['key']) == before
    await ctl.handle(event('Create the plan and execute it'))
    assert ctl.store.get(before['key']) == before
    await ctl.handle(event('!interview resume'))
    after = ctl.store.get(before['key'])
    assert after['pending']['kind'] == 'budget' and after['pending']['choices'] == CHOICES
    assert after['pending']['nonce'] != before['pending']['nonce']
    assert after['messages'] == MESSAGES and after['budget'] == before['budget']
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_controller_timeout_and_lease_are_doubled(tmp_path, monkeypatch):
    import asyncio
    import time
    from contextlib import asynccontextmanager
    ctl, _, model = setup_controller(tmp_path, [])
    limits = []
    @asynccontextmanager
    async def timeout(seconds):
        limits.append(seconds)
        yield
    monkeypatch.setattr(asyncio, 'timeout', timeout)
    async def turn(rec, *args, **kwargs):
        remaining = rec['busy_until'] - time.time()
        assert 298 < remaining <= 300
        assert remaining > limits[-1]
        return budget()
    model.side_effect = turn
    await ctl.handle(event('!interview Research this'))
    assert limits == [240]


@pytest.mark.asyncio
@pytest.mark.parametrize('bad', ['owner', 'team', 'channel', 'thread', 'message_id', 'nonce', 'token'])
async def test_budget_rejects_foreign_stale_or_invalid_actions(tmp_path, bad):
    ctl, _, model = setup_controller(tmp_path, [budget()])
    await ctl.handle(event('!interview Research this'))
    before = ctl.store.get(ctl.key(event('')))
    args = dict(key=before['key'], nonce=before['pending']['nonce'], token='0',
                owner='U1', team='T1', channel='C1', thread='th1', message_id='card1')
    args[bad] = 'wrong'
    await ctl.accept(**args)
    assert ctl.store.get(before['key']) == before
    assert model.await_count == 1


@pytest.mark.asyncio
async def test_failed_budget_delivery_is_recoverable_without_model_call(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [budget('timeout')])
    ui.send_clarify.return_value = SendResult(success=False, error='offline')
    with pytest.raises(RuntimeError):
        await ctl.handle(event('!interview Research this'))
    before = ctl.store.get(ctl.key(event('')))
    assert before['phase'] == 'paused' and before['pending']['kind'] == 'budget'
    assert before['messages'] == MESSAGES and before['busy_until'] == 0
    ctl = InterviewController(ctl.runner, InterviewStore(tmp_path / 'interviews.sqlite3'), ctl.bank_loader)
    ui.send_clarify.return_value = SendResult(success=True, message_id='retry')
    await ctl.handle(event('!interview resume'))
    after = ctl.store.get(before['key'])
    assert after['pending']['message_id'] == 'retry'
    assert after['pending']['nonce'] != before['pending']['nonce']
    assert model.await_count == 1
