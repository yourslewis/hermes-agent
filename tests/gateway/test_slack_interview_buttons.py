"""Durable interview button path does not rely on ordinary clarify memory."""
from unittest.mock import AsyncMock
import pytest
from tests.gateway.test_slack_clarify_buttons import _make_adapter
from tests.gateway.test_interview_routing import event, setup_controller


@pytest.mark.asyncio
async def test_durable_button_after_restart_routes_before_in_memory_guard(tmp_path):
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='question', question='Audience?', choices=['Team','Public'], messages=[]),
        dict(kind='summary', text='Summary', messages=[])])
    await ctl.handle(event('/interview Dashboard'))
    rec = ctl.store.get(ctl.key(event('')))
    adapter = _make_adapter()
    class Runner:
        _interview_controller = ctl
        def _is_user_authorized(self, source): return True
        async def handle(self, event): return None
    adapter.set_message_handler(Runner().handle)
    # Deliberately empty: old process's in-memory clarify guard is gone.
    adapter._clarify_resolved.clear()
    body = {'team': {'id': 'T1'}, 'channel': {'id': 'C1'}, 'user': {'id': 'U1', 'name': 'user'},
        'message': {'ts': 'card1', 'thread_ts': 'th1'}}
    action = {'action_id': 'hermes_clarify_choice_0',
        'value': 'iv:' + rec['key'] + ':' + rec['pending']['nonce'] + '|0'}
    ack = AsyncMock()
    await adapter._handle_clarify_action(ack, body, action)
    ack.assert_awaited_once()
    assert model.await_count == 2
    await adapter._handle_clarify_action(ack, body, action)
    assert model.await_count == 2


@pytest.mark.asyncio
async def test_nonowner_reset_cannot_cancel_owner_at_adapter_guard(tmp_path):
    import asyncio
    from gateway.session import build_session_key
    ctl, ui, model = setup_controller(tmp_path, [dict(kind='question', question='Who?', choices=[], messages=[])])
    await ctl.handle(event('/interview Dashboard'))
    adapter = _make_adapter()
    class Runner:
        _interview_controller = ctl
        def _is_user_authorized(self, source): return True
        async def handle(self, incoming):
            handled, response = await ctl.handle(incoming)
            return response
    adapter.set_message_handler(Runner().handle)
    source_event = event('/reset', user='U2')
    key = build_session_key(source_event.source)
    owner_task = asyncio.create_task(asyncio.Event().wait())
    adapter._active_sessions[key] = asyncio.Event()
    adapter._session_tasks[key] = owner_task
    adapter.send = AsyncMock()
    try:
        await adapter.handle_message(source_event)
        assert not owner_task.cancelled()
        assert not owner_task.done()
        assert ctl.store.get(ctl.key(source_event))['phase'] == 'awaiting_answer'
    finally:
        owner_task.cancel()
        await asyncio.gather(owner_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_open_question_and_summary_use_literal_thread_delivery_not_slash_fallback():
    adapter = _make_adapter()
    adapter.send = AsyncMock(side_effect=AssertionError('generic slash/media path forbidden'))
    adapter._team_clients['T1'].chat_postMessage.return_value = {'ok': True, 'ts': '1740957123.456'}
    result = await adapter.send_interview_text('C1', '<!channel> literal',
        metadata={'thread_id': 'th1', 'team_id': 'T1'})
    assert result.success and result.message_id == '1740957123.456'
    kwargs = adapter._team_clients['T1'].chat_postMessage.call_args.kwargs
    assert kwargs['thread_ts'] == 'th1'
    assert kwargs['mrkdwn'] is False
    assert kwargs['unfurl_links'] is False
    assert kwargs['blocks'][0]['text']['text'] == '<!channel> literal'


@pytest.mark.asyncio
async def test_choice_card_honors_explicit_workspace_over_channel_cache():
    adapter = _make_adapter()
    adapter._team_clients['T2'] = AsyncMock()
    adapter._team_clients['T2'].chat_postMessage.return_value = {'ok':True, 'ts':'secondary'}
    adapter._team_clients['T1'].chat_postMessage.return_value = {'ok':True, 'ts':'primary'}
    adapter._ensure_dm_conversation = AsyncMock(return_value='C1')
    result = await adapter.send_clarify('C1', 'Choose', ['A'], 'iv:key:nonce', 'session',
        metadata={'thread_id':'thread', 'team_id':'T2'})
    assert result.message_id == 'secondary'
    adapter._team_clients['T1'].chat_postMessage.assert_not_awaited()
