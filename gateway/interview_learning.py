"""Narrow learning sidecar; it never supplies an answer or changes routing authority."""
import logging
from pathlib import Path
from hermes_constants import get_hermes_home

log = logging.getLogger(__name__)

async def learning_control(home, source, command):
    """Explicit authenticated controls; no question text can toggle learning."""
    import json
    import os
    import time
    import fcntl
    config_path = Path(home) / 'question-learning.json'
    try:
        import stat
        from agent.interview_questions import _directory_path
        with _directory_path(Path(home)) as directory:
            fd = os.open(config_path.name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, 'r+') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Invalid configuration')
            fcntl.flock(stream, fcntl.LOCK_EX)
            raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError('Invalid configuration')
            config = json.loads(raw)
            if not isinstance(config, dict):
                raise ValueError('Invalid configuration')
            if (config.get('profile') != 'rex' or config.get('owner') != source.user_id
                    or config.get('team') != source.scope_id
                    or source.chat_id not in config.get('channels', [])):
                return 'Question learning access denied.'
            if command in {'pause', 'enable'}:
                config['enabled'] = command == 'enable'
                if command == 'enable':
                    config['since'] = f'{time.time():.6f}'
                stream.seek(0)
                json.dump(config, stream, indent=2)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
                return 'Question learning enabled for new messages.' if config['enabled'] else 'Question learning paused; existing questions retained.'
            if command == 'status':
                return ('Question learning: ' + ('enabled' if config.get('enabled') is True else 'paused')
                    + '. Edit questions and review proposals in Notes → Question Bank. Automatically created questions carry auto-generated.')
            if command == 'digest':
                from agent.interview_learning import digest
                return digest(home) or 'No new question-learning changes.'
            if command.startswith('undo '):
                from agent.interview_learning import undo
                result = undo(home, command[len('undo '):].strip())
                return 'Question learning undo: ' + result['status'] + '. Manual edits are never overwritten.'
            return 'Use !interview learning status, pause, enable, digest, or undo QUESTION_ID. Review proposals in Notes.'
    except (OSError, ValueError):
        return 'Question learning is not configured or its configuration is invalid.'

async def learn_message(*args, **kwargs):
    # Lazy import keeps disabled profiles independent of learning dependencies.
    from agent.interview_learning import learn_message as implementation
    return await implementation(*args, **kwargs)

async def capture_learning(adapter, event, source, text):
    home = Path(get_hermes_home())
    if not event.get('user') or event['user'] != source.user_id:
        return  # A thread owner's metadata is not proof of message authorship.
    if home.name != 'rex' or event.get('_hermes_force_process') or event.get('subtype'):
        return
    if not isinstance(text, str) or not text.strip():
        return
    try:
        if adapter._event_declares_bot_sender(event) or await adapter._resolve_user_is_bot(
                source.user_id, chat_id=source.chat_id, team_id=source.scope_id):
            return
        await learn_message(home, {'profile': home.name, 'user_id': source.user_id,
            'team': source.scope_id, 'channel': source.chat_id, 'thread': source.thread_id or ''},
            text, event.get('ts', ''), internal=False)
        from gateway.question_learning_digest import deliver_digest
        await deliver_digest(adapter, home, source)
    except Exception:
        # No exception text or original message enters logs; ordinary/interview
        # processing must remain usable if the optional learner is unavailable.
        log.warning('Question learning failed; primary message handling continues.')
