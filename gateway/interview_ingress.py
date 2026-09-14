"""Minimal Slack interview ingress, before context hydration and media reads."""
from gateway.interview import get_controller
from gateway.platforms.base import MessageEvent


async def route_slack_interview(adapter, event, payload, text):
    """Return true when this event must not enter ordinary Slack ingress."""
    runner = getattr(getattr(adapter, '_message_handler', None), '__self__', None)
    if runner is None:
        return False
    team_id = adapter._event_team_id(event, payload)
    channel_id = event.get('channel', '')
    # Transport identity metadata only: never load the assistant's view/context.
    metadata = adapter._lookup_assistant_thread_metadata(
        event, channel_id=channel_id, thread_ts=event.get('thread_ts', ''),
        team_id=team_id, body=payload)
    channel_id = channel_id or metadata.get('channel_id', '')
    team_id = team_id or metadata.get('team_id') or adapter._channel_team.get(channel_id, '')
    # Without workspace identity we cannot prove this is outside a persisted
    # interview. Never hydrate a potentially restricted thread under a new key.
    if not team_id:
        return True
    channel_type = event.get('channel_type') or ('im' if channel_id.startswith('D') else '')
    thread_id = event.get('thread_ts') or metadata.get('thread_ts')
    if channel_type in {'im', 'mpim'}:
        if not thread_id and adapter._dm_top_level_threads_as_sessions():
            thread_id = event.get('ts')
    elif not event.get('_hermes_no_thread_response'):
        if not thread_id or thread_id == event.get('ts'):
            thread_id = event.get('ts') if adapter.config.extra.get('reply_in_thread', True) else None
    source = adapter.build_source(
        chat_id=channel_id, user_id=event.get('user') or metadata.get('user_id', ''),
        chat_type='dm' if channel_type in {'im', 'mpim'} else 'group',
        thread_id=thread_id, scope_id=team_id)
    from plugins.platforms.slack.adapter import (
        _rewrite_known_bang_command, _slack_mention_detection_text,
    )
    bot_uid = adapter._team_bot_user_ids.get(source.scope_id, adapter._bot_user_id)
    routing_text = _slack_mention_detection_text(event) or text
    mentioned = bool((bot_uid and f'<@{bot_uid}>' in routing_text)
                     or adapter._slack_message_matches_mention_patterns(routing_text))
    if not source.user_id or not runner._is_user_authorized(source):
        return True
    is_dm = source.chat_type == 'dm'
    if is_dm and adapter._slack_disable_dms():
        return True
    if channel_type != 'im' and bot_uid:
        allowed = adapter._slack_allowed_channels()
        if allowed and source.chat_id not in allowed:
            return True
        self_uids = {uid for uid in (bot_uid, adapter._bot_user_id) if uid}
        if (adapter._slack_ignore_other_user_mentions() and not mentioned
                and not adapter._slack_message_mentions_self(routing_text, self_uids)
                and adapter._slack_message_addressed_to_other_user(routing_text, self_uids)):
            return True
        free_response = (
            source.chat_id not in adapter._slack_require_mention_channels()
            and (source.chat_id in adapter._slack_free_response_channels()
                 or not adapter._slack_require_mention()))
        thread_reply = bool(source.thread_id and source.thread_id != event.get('ts'))
        if not mentioned and not event.get('_hermes_force_process') and (
            (adapter._slack_strict_mention() and not free_response)
            or (adapter._slack_thread_require_mention() and thread_reply)
        ):
            return True
    text = _rewrite_known_bang_command(text.replace(f'<@{bot_uid}>', '').strip())
    if text == '/hermes interview' or text.startswith('/hermes interview '):
        text = '/' + text[len('/hermes '):]
    command = text == '/interview' or text.startswith('/interview ')
    message = MessageEvent(text=text, source=source, message_id=event.get('ts', ''),
                           internal=bool(event.get('_hermes_force_process')))
    try:
        controller = get_controller(runner)
        rec = controller.store.get(controller.key(message))
    except Exception:
        import logging
        logging.getLogger(__name__).exception('Interview ingress state lookup failed closed')
        return True
    if not command and (not rec or rec['phase'] == 'exited'):
        return False
    sender_is_bot = adapter._event_declares_bot_sender(event)
    if not sender_is_bot and source.user_id != bot_uid:
        sender_is_bot = await adapter._resolve_user_is_bot(
            source.user_id, chat_id=source.chat_id, team_id=source.scope_id)
    if source.user_id == bot_uid or (sender_is_bot and (
        adapter._slack_allow_bots() == 'none'
        or (adapter._slack_allow_bots() == 'mentions' and not mentioned)
    )):
        return True
    from gateway.interview_learning import capture_learning
    await capture_learning(adapter, event, source, text)
    await adapter.handle_message(message)
    return True
