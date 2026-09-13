"""Exercise real Slack ingress before ordinary context/media side effects."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.interview import InterviewController
from gateway.interview_store import InterviewStore
from gateway.platforms.base import MessageEvent, SendResult
from tests.gateway.test_slack_clarify_buttons import _make_adapter


class Runner:
    def __init__(self, adapter, tmp_path):
        self._is_user_authorized = Mock(return_value=True)
        self._running_agents = {}
        self._interview_controller = InterviewController(
            self, InterviewStore(tmp_path / 'interviews.sqlite3'),
            lambda: {'version': 'test', 'prompt': '', 'questions': []})
        self._interview_turn = AsyncMock(return_value={
            'kind': 'question', 'question': 'Who?', 'choices': [], 'messages': []})
        self.adapters = {adapter.platform: adapter}

    def _session_key_for_source(self, source):
        return 'normal'

    async def handle(self, event):
        handled, response = await self._interview_controller.handle(event)
        assert handled, 'interview escaped into ordinary dispatch'
        return response


def setup_ingress(tmp_path):
    adapter = _make_adapter()
    runner = Runner(adapter, tmp_path)
    adapter.set_message_handler(runner.handle)
    adapter._resolve_user_is_bot = AsyncMock(return_value=False)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id='sent'))
    adapter.send_clarify = AsyncMock(return_value=SendResult(success=True, message_id='card'))
    adapter.handle_message = AsyncMock(wraps=adapter.handle_message)
    # Any ordinary ingress effects are forbidden, including cached history
    # checks and watermarks, not merely network requests.
    for name in ('_should_wake_on_unmentioned_message', '_fetch_thread_context',
                 '_collect_thread_root_images', '_download_slack_file'):
        setattr(adapter, name, AsyncMock(side_effect=AssertionError(name)))
    for name in ('_has_active_session_for_thread', '_get_thread_watermark',
                 '_set_thread_watermark', '_mark_thread_rehydration_checked',
                 '_agent_view_context_for_event', '_register_mentioned_thread'):
        setattr(adapter, name, Mock(side_effect=AssertionError(name)))
    return adapter, runner


def slack_event(text='My answer', **kwargs):
    return dict(type='message', channel='C1', channel_type='channel', team='T1',
                user='U1', ts='2.000', thread_ts='1.000', client_msg_id='human',
                text=text, **kwargs)


def seed_interview(adapter, runner):
    source = adapter.build_source(chat_id='C1', chat_type='group', user_id='U1',
                                  thread_id='1.000', scope_id='T1')
    controller = runner._interview_controller
    event = MessageEvent(text='', source=source)
    rec = controller.store.create(controller.key(event), 'U1', 'Dashboard',
                                  controller.bank_loader(), controller.source(event))
    controller.store.update(rec['key'], rec['revision'], phase='awaiting_answer',
        pending={'question': 'Who?', 'choices': [], 'kind': 'answer',
                 'nonce': 'n', 'message_id': 'card', 'awaiting_text': True})
    return rec['key']


@pytest.mark.asyncio
async def test_active_interview_routes_before_context_or_watermarks(tmp_path):
    adapter, runner = setup_ingress(tmp_path)
    key = seed_interview(adapter, runner)
    await adapter._handle_slack_message(slack_event(
        files=[{'id': 'F1', 'url_private': 'https://example.invalid/private'}],
        attachments=[{'text': 'UNTRUSTED PREVIEW'}],
        blocks=[{'type': 'section', 'text': {'type': 'mrkdwn', 'text': 'QUOTED CONTENT'}}]))
    adapter.handle_message.assert_awaited_once()
    delivered = adapter.handle_message.await_args.args[0]
    assert delivered.text == 'My answer'
    assert delivered.channel_context is None
    assert delivered.media_urls == []
    assert delivered.raw_message is None
    assert delivered.source.scope_id == 'T1'
    assert delivered.source.thread_id == '1.000'
    assert runner._interview_controller.store.get(key)['answers'][-1]['answer'] == 'My answer'


@pytest.mark.asyncio
async def test_corrupt_state_fails_closed_before_context(tmp_path):
    adapter, runner = setup_ingress(tmp_path)
    runner._interview_controller.store.get = Mock(side_effect=ValueError('corrupt state'))
    await adapter._handle_slack_message(slack_event())
    adapter.handle_message.assert_not_awaited()
    runner._interview_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['/interview Dashboard', '<@U_BOT> /interview Dashboard',
                                 '<@U_BOT> !interview Dashboard', '/hermes interview Dashboard'])
async def test_entry_command_skips_hydration(tmp_path, text):
    adapter, runner = setup_ingress(tmp_path)
    await adapter._handle_slack_message(slack_event(text))
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks)
    adapter.handle_message.assert_awaited_once()
    runner._interview_turn.assert_awaited_once()
    assert runner._interview_turn.await_args.args[0]['task'] == 'Dashboard'


@pytest.mark.asyncio
@pytest.mark.parametrize('active', [False, True])
@pytest.mark.parametrize('policy', ['strict_mention', 'thread_require_mention',
                                   'allowed_channels', 'unauthorized', 'bot',
                                   'disable_dms', 'ignore_other_user_mentions'])
async def test_interview_ingress_preserves_admission_policy(tmp_path, active, policy):
    adapter, runner = setup_ingress(tmp_path)
    if active:
        seed_interview(adapter, runner)
    event = slack_event('My answer' if active else '/interview Dashboard')
    if policy == 'unauthorized':
        runner._is_user_authorized.return_value = False
    elif policy == 'bot':
        adapter._resolve_user_is_bot.return_value = True
    elif policy == 'allowed_channels':
        adapter.config.extra[policy] = ['C_OTHER']
    elif policy == 'disable_dms':
        adapter.config.extra[policy] = True
        event['channel_type'] = 'im'
    else:
        adapter.config.extra[policy] = True
        if policy == 'ignore_other_user_mentions':
            event['text'] = '<@U_OTHER> ' + event['text']
    await adapter._handle_slack_message(event)
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks)
    adapter.handle_message.assert_not_awaited()
    runner._interview_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('location', ['outer_payload', 'channel_cache', 'assistant_metadata'])
async def test_workspace_metadata_resolves_before_interview_lookup(tmp_path, location):
    adapter, runner = setup_ingress(tmp_path)
    seed_interview(adapter, runner)
    event = slack_event()
    event.pop('team')
    payload = {'team_id': 'T1'} if location == 'outer_payload' else None
    if location == 'assistant_metadata':
        adapter._channel_team.clear()
        adapter._lookup_assistant_thread_metadata = Mock(return_value={
            'team_id': 'T1', 'channel_id': 'C1', 'user_id': 'U1', 'thread_ts': '1.000'})
        event.pop('user')
        event.pop('thread_ts')
    await adapter._handle_slack_message(event, payload)
    runner._interview_turn.assert_awaited_once()
    delivered = adapter.handle_message.await_args.args[0]
    assert delivered.source.scope_id == 'T1'
    assert delivered.source.user_id == 'U1'
    assert delivered.source.thread_id == '1.000'


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['top_level', 'internal', 'missing_team'])
async def test_transport_scope_is_preserved_without_synthetic_execution(tmp_path, kind):
    adapter, runner = setup_ingress(tmp_path)
    event = slack_event('/interview Dashboard')
    if kind == 'top_level':
        event.pop('thread_ts')
    elif kind == 'internal':
        event['_hermes_force_process'] = True
    else:
        event.pop('team')
        adapter._channel_team.clear()
    await adapter._handle_slack_message(event)
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks)
    if kind == 'top_level':
        runner._interview_turn.assert_awaited_once()
        assert adapter.handle_message.await_args.args[0].source.thread_id == '2.000'
    else:
        runner._interview_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_inferred_private_dm_remains_mention_exempt(tmp_path):
    adapter, runner = setup_ingress(tmp_path)
    adapter.config.extra['strict_mention'] = True
    event = slack_event('/interview Dashboard')
    event['channel'] = 'D1'
    event.pop('channel_type')
    await adapter._handle_slack_message(event)
    if adapter._background_tasks:
        await asyncio.gather(*adapter._background_tasks)
    runner._interview_turn.assert_awaited_once()


@pytest.mark.asyncio
async def test_ordinary_internal_message_retains_existing_wake_policy(tmp_path):
    from gateway.interview_ingress import route_slack_interview
    adapter, runner = setup_ingress(tmp_path)
    adapter.config.extra['strict_mention'] = True
    event = slack_event('normal reaction handoff')
    event['_hermes_force_process'] = True
    assert await route_slack_interview(adapter, event, None, event['text']) is False


@pytest.mark.asyncio
async def test_unresolved_workspace_cannot_escape_persisted_interview(tmp_path):
    adapter, runner = setup_ingress(tmp_path)
    seed_interview(adapter, runner)
    event = slack_event()
    event.pop('team')
    adapter._channel_team.clear()
    await adapter._handle_slack_message(event)
    adapter.handle_message.assert_not_awaited()
    runner._interview_turn.assert_not_awaited()
