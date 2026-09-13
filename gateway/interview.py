"""Durable, owner-bound interview routing, isolated from the ordinary agent."""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import uuid
import time
from pathlib import Path
from gateway.config import Platform

log = logging.getLogger(__name__)
PLAN_QUESTION = 'Would you like a multi-step, multi-agent implementation plan based on these requirements?'
PLAN_CHOICES = ['Create the plan', 'Revise requirements', 'Stop here']


def get_controller(runner):
    controller = getattr(runner, '_interview_controller', None)
    if controller is None:
        from hermes_constants import get_hermes_home
        from gateway.interview_store import InterviewStore
        from agent.interview_questions import load_question_bank
        controller = InterviewController(runner,
            InterviewStore(Path(get_hermes_home()) / 'interviews.sqlite3'), load_question_bank)
        runner._interview_controller = controller
    return controller


async def route_interview(runner, event):
    return await get_controller(runner).handle(event)


async def adapter_interview_admission(adapter, event):
    runner = getattr(getattr(adapter, '_message_handler', None), '__self__', None)
    if runner is None:
        return False  # Multiplex entry is separately refused by the core router.
    try:
        controller = get_controller(runner)
        rec = controller.store.get(controller.key(event))
        if not rec or rec['phase'] == 'exited':
            # Busy entry is refused, not queued until existing execution ends.
            if (event.text or '').startswith('/interview'):
                from gateway.session import build_session_key
                lane = build_session_key(event.source,
                    group_sessions_per_user=adapter.config.extra.get('group_sessions_per_user', True),
                    thread_sessions_per_user=adapter.config.extra.get('thread_sessions_per_user', False))
                if lane in adapter._active_sessions:
                    await adapter.send(chat_id=event.source.chat_id,
                        content='This thread is busy; stop existing execution before starting an interview.',
                        metadata={'thread_id': event.source.thread_id, 'team_id': event.source.scope_id})
                    return True
            return False
        handled, response = await controller.handle(event)
    except Exception:
        log.exception('Interview adapter admission failed closed')
        handled, response = True, 'Interview state unavailable; dispatch stopped safely.'
    if handled and response:
        await adapter.send(chat_id=event.source.chat_id, content=response,
            metadata={'thread_id': event.source.thread_id, 'team_id': event.source.scope_id})
    return handled


async def handle_interview_action(adapter, body, clarify_id, token):
    runner = getattr(getattr(adapter, '_message_handler', None), '__self__', None)
    if runner is None:
        return
    channel = body.get('channel', {}).get('id', '')
    message = body.get('message', {})
    team = body.get('team', {}).get('id', '')
    owner = body.get('user', {}).get('id', '')
    try:
        prefix, key, nonce = clarify_id.split(':')
        controller = get_controller(runner)
        outcome = await controller.accept(key, nonce, token, owner, team, channel,
            message.get('thread_ts', ''), message.get('ts', ''))
    except Exception:
        log.exception('Interview action failed; retained restricted state')
        outcome = 'Interview action failed safely. Use /interview status or resume.'
    # Keep status private to the actor; stale/foreign clicks must not rewrite
    # a live card belonging to somebody else.
    await adapter._get_client(channel, team_id=team or None).chat_postEphemeral(
        channel=channel, user=owner, thread_ts=message.get('thread_ts'), text=outcome)


class InterviewController:
    def __init__(self, runner, store, bank_loader):
        self.runner, self.store, self.bank_loader = runner, store, bank_loader
        self.locks = {}

    @staticmethod
    def source(event):
        s = event.source
        return {'team': s.scope_id or s.guild_id or '', 'channel': s.chat_id,
                'thread': s.thread_id or '', 'profile': s.profile or ''}

    @classmethod
    def key(cls, event):
        return hashlib.sha256(json.dumps(cls.source(event), sort_keys=True).encode()).hexdigest()

    def adapter(self):
        return self.runner.adapters[Platform.SLACK]

    @staticmethod
    def metadata(rec):
        return {'thread_id': rec['source']['thread'], 'team_id': rec['source']['team']}

    async def handle(self, event):
        if event.source.platform != Platform.SLACK:
            return False, None
        key = self.key(event)
        text = (event.text or '').strip()
        if text.startswith('/hermes interview'):
            text = text[len('/hermes '):]
            text = '/' + text
        command = text == '/interview' or text.startswith('/interview ')
        rec = self.store.get(key)
        active = rec is not None and rec['phase'] != 'exited'
        if not active and not command:
            return False, None
        if getattr(getattr(self.runner, 'config', None), 'multiplex_profiles', False):
            return True, 'Interview mode is unavailable in multiplex gateways; use a dedicated profile gateway.'
        if getattr(event, 'internal', False):
            return True, None
        if not self.runner._is_user_authorized(event.source) or not event.source.user_id:
            return True, 'Interview access denied.'
        access_check = getattr(self.runner, '_check_slash_access', None)
        if command and access_check is not None:
            denial = access_check(event.source, 'interview')
            if denial is not None:
                return True, denial
        if active and event.source.user_id != rec['owner']:
            return True, 'Only the interview owner can answer or change this interview.'
        if not event.source.thread_id:
            return True, 'Start /interview inside a Slack thread, so its restrictions have an unambiguous scope.'
        lock = self.locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return True, 'Interview turn is in progress. No task execution is enabled; please retry the control after it finishes.'
        async with lock:
            # A persisted lease covers a second gateway/process, not just this
            # event loop. Expiry only permits explicit resume; never execution.
            if active and rec.get('busy_until', 0) > time.time():
                return True, 'Interview turn is in progress. Please retry shortly.'
            if not active:
                task = text[len('/interview'):].strip()
                if not task or task in {'status', 'finish', 'resume', 'exit'}:
                    return True, 'Start with /interview <task> in this thread.'
                # Never pretend entering a mode cancels an existing execution.
                session_key = self.runner._session_key_for_source(event.source)
                if getattr(self.runner, '_running_agents', {}).get(session_key):
                    return True, 'This thread has active execution. Stop it before starting an interview.'
                roots = []
                if task.startswith('--read-root'):
                    import shlex
                    options, separator, task = task.partition(' -- ')
                    tokens = shlex.split(options)
                    if not separator or len(tokens) % 2 or any(tokens[i] != '--read-root' for i in range(0, len(tokens), 2)):
                        return True, 'Use /interview --read-root "/absolute/project" -- <task>.'
                    for raw in tokens[1::2]:
                        root = Path(raw).expanduser()
                        if not root.is_absolute() or not root.is_dir() or root.is_symlink():
                            return True, 'Each read root must be an existing absolute directory, not a symlink.'
                        roots.append(str(root.resolve()))
                    if not task.strip():
                        return True, 'An interview task is required.'
                rec = self.store.create(key, event.source.user_id, task, self.bank_loader(), self.source(event))
                rec = self.store.update(key, rec['revision'], read_roots=roots)
                await self.advance(rec, task)
                return True, None
            return await self._active(rec, text, command, event.message_id)

    async def _active(self, rec, text, command, message_id=None):
        if command:
            control = text[len('/interview'):].strip()
            if control == 'status':
                return True, f"Interview: {rec['phase']}. Task execution is disabled."
            if control == 'exit':
                self.store.update(rec['key'], rec['revision'], phase='exited', pending=None)
                return True, 'Interview exited. Nothing was executed; send a separate request to do work.'
            if control == 'resume' and rec.get('delivery'):
                await self.deliver_output(rec)
                return True, None
            if control == 'resume' and rec.get('pending'):
                pending = rec['pending']
                await self.ask(rec, pending['question'], pending['choices'], kind=pending['kind'])
                return True, None
            if control in {'finish', 'resume'}:
                rec = self.store.update(rec['key'], rec['revision'], pending=None, phase='collecting')
                await self.advance(rec, 'Summarize requirements and unresolved items now.' if control == 'finish' else 'Resume clarification; ask what needs clarification or revision.',
                    intent='summary' if control == 'finish' else 'collect')
                return True, None
            return True, 'Use /interview status, finish, resume, or exit.'
        if text.startswith('/'):
            if text.split()[0] in {'/stop', '/reset', '/new'}:
                self.store.update(rec['key'], rec['revision'], pending=None, phase='paused')
                return True, 'Interview paused; restrictions remain. Use /interview resume or exit.'
            return True, 'That command is disabled during interview mode. Use /interview exit first.'
        if rec['phase'] in {'awaiting_plan_decision', 'complete', 'plan_complete'}:
            return True, 'Execution remains disabled. Use the planning buttons or /interview resume, finish, or exit.'
        pending = rec.get('pending')
        if rec['phase'] == 'awaiting_answer' and pending and text:
            consumed = rec.get('consumed_messages', [])
            if not message_id:
                return True, 'Cannot safely identify this answer. Please send it as a new thread message.'
            if message_id in consumed:
                return True, 'That answer was already recorded.'
            rec = self.store.update(rec['key'], rec['revision'], pending=None, phase='collecting',
                consumed_messages=consumed + [message_id],
                answers=rec['answers'] + [{'question': pending['question'], 'answer': text}])
            await self.advance(rec, text)
            return True, None
        return True, 'Interview is paused. Use /interview resume or finish.'

    async def accept(self, key, nonce, token, owner, team, channel, thread, message_id):
        lock = self.locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return 'Interview is busy; try again.'
        async with lock:
            rec = self.store.get(key)
            if rec is None or rec['owner'] != owner or rec['source']['team'] != team or rec['source']['channel'] != channel or rec['source']['thread'] != thread:
                return 'Interview answer rejected: wrong owner or conversation.'
            pending = rec.get('pending')
            if not pending or pending['nonce'] != nonce or pending['message_id'] != message_id or rec['phase'] not in {'awaiting_answer','awaiting_plan_decision'}:
                return 'This interview question is expired or already answered.'
            if token == 'other':
                pending = dict(pending, awaiting_text=True)
                self.store.update(key, rec['revision'], pending=pending)
                return 'Type your answer in this thread. For planning, use /interview resume to revise requirements; Other never authorizes planning.'
            if not token.isdigit() or int(token) >= len(pending['choices']):
                return 'Invalid interview answer.'
            answer = pending['choices'][int(token)]
            rec = self.store.update(key, rec['revision'], pending=None, phase='collecting',
                answers=rec['answers'] + [{'question': pending['question'], 'answer': answer}])
            if pending['kind'] == 'plan':
                if token == '0':
                    await self.advance(rec, 'Create the multi-step multi-agent plan, but do not execute it.', intent='plan')
                elif token == '1':
                    await self.advance(rec, 'Revise the requirements; ask what should change.')
                else:
                    self.store.update(key, rec['revision'], phase='complete')
            else:
                await self.advance(rec, answer)
            return 'Answer recorded.'

    async def advance(self, rec, text, intent='collect'):
        rec = self.store.update(rec['key'], rec['revision'], busy_until=time.time() + 150,
            phase='planning' if intent == 'plan' else 'collecting')
        try:
            async with asyncio.timeout(120):
                return await self._advance(rec, text, intent)
        except BaseException:
            # Cancellation/timeout/crash never restores ordinary routing.
            current = self.store.get(rec['key'])
            if current and current['phase'] != 'exited':
                self.store.update(current['key'], current['revision'], phase='paused', busy_until=0)
            raise
        finally:
            current = self.store.get(rec['key'])
            if current and current['phase'] != 'exited' and current.get('busy_until'):
                self.store.update(current['key'], current['revision'], busy_until=0)

    async def _advance(self, rec, text, intent='collect'):
        model = getattr(self.runner, '_interview_turn', None)
        if model is None:
            from agent.interview_runtime import run_interview_turn
            model = run_interview_turn
        result = await model(rec, text, intent=intent)
        rec = self.store.update(rec['key'], rec['revision'], messages=result['messages'])
        if result['kind'] == 'question':
            await self.ask(rec, result['question'], result['choices'])
        else:
            is_plan = intent == 'plan'
            text = result['text']
            rec = self.store.update(rec['key'], rec['revision'],
                delivery={'kind': 'plan' if is_plan else 'summary', 'text': text},
                **({'plan': text, 'phase': 'plan_complete'} if is_plan else {'summary': text, 'phase': 'complete'}))
            await self.deliver_output(rec)

    async def deliver_output(self, rec):
        # Durable outbox: a crash may duplicate text on resume, but can never
        # silently skip a requirements summary or grant planning authorization.
        delivery = rec['delivery']
        sent = await self.adapter().send_interview_text(chat_id=rec['source']['channel'],
            content=delivery['text'], metadata=self.metadata(rec))
        if not sent.success:
            self.store.update(rec['key'], rec['revision'], phase='paused')
            raise RuntimeError('Interview summary delivery failed; no plan authorized.')
        if delivery['kind'] == 'summary':
            await self.ask(rec, PLAN_QUESTION, PLAN_CHOICES, kind='plan')
            rec = self.store.get(rec['key'])
        self.store.update(rec['key'], rec['revision'], delivery=None,
            phase='plan_complete' if delivery['kind'] == 'plan' else 'awaiting_plan_decision')

    async def ask(self, rec, question, choices, kind='answer'):
        pending = {'nonce': uuid.uuid4().hex, 'question': question, 'choices': choices,
                   'kind': kind, 'message_id': '', 'awaiting_text': not bool(choices)}
        rec = self.store.update(rec['key'], rec['revision'],
            phase='awaiting_plan_decision' if kind == 'plan' else 'awaiting_answer', pending=pending)
        result = await self.adapter().send_clarify(chat_id=rec['source']['channel'], question=question,
            choices=choices, clarify_id='iv:' + rec['key'] + ':' + pending['nonce'],
            session_key=rec['id'], metadata=self.metadata(rec))
        if not result.success or not isinstance(result.message_id, str) or not result.message_id:
            self.store.update(rec['key'], rec['revision'], phase='paused')
            raise RuntimeError('Interview question delivery failed; remains restricted.')
        pending['message_id'] = result.message_id
        self.store.update(rec['key'], rec['revision'], pending=pending)
