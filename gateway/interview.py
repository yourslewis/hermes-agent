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
            if (event.text or '').strip().startswith(('/interview', '!interview', '/hermes interview')):
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
        outcome = 'Interview action failed safely. Use !interview status or resume.'
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
        if text == '!interview' or text.startswith('!interview '):
            text = '/' + text[1:]
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
        if active and event.source.user_id != rec['owner']:
            return True, 'Only the interview owner can answer or change this interview.'
        access_check = getattr(self.runner, '_check_slash_access', None)
        if command and access_check is not None:
            denial = access_check(event.source, 'interview')
            if denial is not None:
                return True, denial
        if not event.source.thread_id:
            return True, 'Start !interview inside a Slack thread, so its restrictions have an unambiguous scope.'
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
                if not task or task.split()[0] in {'status', 'finish', 'resume', 'exit', 'approve-read', 'approve-history'}:
                    return True, 'Start with !interview [--project /absolute/project --] <task> in this thread.'
                # Never pretend entering a mode cancels an existing execution.
                session_key = self.runner._session_key_for_source(event.source)
                if getattr(self.runner, '_running_agents', {}).get(session_key):
                    return True, 'This thread has active execution. Stop it before starting an interview.'
                roots = []
                if task.startswith('--'):
                    import shlex
                    options, separator, task = task.partition(' -- ')
                    try:
                        tokens = shlex.split(options)
                    except ValueError:
                        return True, 'Use !interview --project "/absolute/project" -- <task>; close all quotes.'
                    if not separator or len(tokens) % 2 or any(tokens[i] not in {'--read-root', '--project'} for i in range(0, len(tokens), 2)):
                        return True, 'Use !interview --project "/absolute/project" -- <task> (--read-root is an alias).'
                    from gateway.interview_permissions import validate_read_root
                    paths = [str(Path.home() / raw[2:]) if raw.startswith('~/') else raw
                             for raw in tokens[1::2]]
                    if any(not Path(raw).is_absolute() for raw in paths):
                        return True, ('--project requires an absolute or ~/ project path. For a project name, '
                            'start !interview <task>, then use !interview approve-read NAME '
                            'to select a directory and confirm read permission.')
                    try:
                        roots = list(dict.fromkeys(validate_read_root(raw) for raw in paths))
                    except ValueError as exc:
                        return True, str(exc)
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
            action, _, argument = control.partition(' ')
            if action == 'approve-read':
                from gateway.interview_permissions import single_argument, validate_read_root, resolve_project_candidates
                try:
                    raw = single_argument(argument)
                    if raw.startswith('~/'):
                        raw = str(Path.home() / raw[2:])
                    if not Path(raw).is_absolute():
                        candidates = resolve_project_candidates(raw, read_roots=rec.get('read_roots', []))
                        if not candidates:
                            return True, 'No safe project match found. Use !interview approve-read /absolute/project; no access was granted.'
                        await self.ask(rec,
                            'Select the exact project directory. Selection does not grant access; '
                            'a separate read permission confirmation follows.',
                            candidates, kind='project_choice')
                        return True, None
                    root = validate_read_root(raw)
                except ValueError as exc:
                    return True, str(exc)
                roots = list(dict.fromkeys(rec.get('read_roots', []) + [root]))
                pending = rec.get('pending')
                resolved = (pending and pending.get('kind') == 'read_permission'
                    and pending.get('path') == root)
                self.store.update(rec['key'], rec['revision'], read_roots=roots,
                    **({'pending': None, 'phase': 'paused'} if resolved else {}))
                return True, f'Read access approved for {root}. Answers retained; continue the interview or use !interview resume.'
            if action == 'approve-history':
                from gateway.interview_permissions import single_argument
                try:
                    from agent.interview_history import validate_history_session
                    session_id = validate_history_session(rec, single_argument(argument))
                except (ValueError, OSError, ImportError) as exc:
                    return True, f'History access denied: {exc}'
                sessions = list(dict.fromkeys(rec.get('history_sessions', []) + [session_id]))
                self.store.update(rec['key'], rec['revision'], history_sessions=sessions)
                return True, f'History session {session_id} approved. Answers retained; use !interview resume to continue.'
            if control == 'status':
                roots = ', '.join(rec.get('read_roots', [])) or 'none'
                sessions = ', '.join(rec.get('history_sessions', [])) or 'none'
                return True, (f"Interview: {rec['phase']}. Task execution is disabled.\n"
                    'Capabilities: web search/extraction and current interview search; '
                    'local reads and prior history only within explicit grants.\n'
                    f'Read roots: {roots}\nHistory sessions: {sessions}')
            if control == 'exit':
                self.store.update(rec['key'], rec['revision'], phase='exited', pending=None)
                return True, 'Interview exited. Nothing was executed; send a separate request to do work.'
            if control == 'resume' and rec.get('delivery'):
                await self.deliver_output(rec)
                return True, None
            if control == 'resume' and rec.get('pending'):
                pending = rec['pending']
                await self.ask(rec, pending['question'], pending['choices'], kind=pending['kind'],
                    path=pending.get('path'))
                return True, None
            if control in {'finish', 'resume'}:
                rec = self.store.update(rec['key'], rec['revision'], pending=None, phase='collecting')
                await self.advance(rec, 'Summarize requirements and unresolved items now.' if control == 'finish' else 'Resume clarification; ask what needs clarification or revision.',
                    intent='summary' if control == 'finish' else 'collect')
                return True, None
            return True, 'Use !interview status, finish, resume, exit, approve-read /absolute/project, or approve-history SESSION_ID.'
        if text.startswith('/'):
            if text.split()[0] in {'/stop', '/reset', '/new'}:
                self.store.update(rec['key'], rec['revision'], pending=None, phase='paused')
                return True, 'Interview paused; restrictions remain. Use !interview resume or exit.'
            return True, 'That command is disabled during interview mode. Use !interview exit first.'
        if rec['phase'] in {'awaiting_plan_decision', 'complete', 'plan_complete'}:
            return True, 'Execution remains disabled. Use the planning buttons or !interview resume, finish, or exit.'
        pending = rec.get('pending')
        if pending and pending['kind'] == 'budget':
            return True, 'Use the owner-bound recovery buttons or !interview resume. Text never authorizes planning or execution.'
        if pending and pending['kind'] == 'project_choice':
            return True, 'Use the owner-bound project selection buttons or !interview approve-read /absolute/project. Text never grants access.'
        if pending and pending['kind'] == 'read_permission':
            return True, 'Use the owner-bound Approve or Deny read access buttons, or !interview approve-read /absolute/project. Text never grants access.'
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
        return True, 'Interview is paused. Use !interview resume or finish.'

    async def accept(self, key, nonce, token, owner, team, channel, thread, message_id):
        lock = self.locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            return 'Interview is busy; try again.'
        async with lock:
            rec = self.store.get(key)
            if rec is None or rec['owner'] != owner or rec['source']['team'] != team or rec['source']['channel'] != channel or rec['source']['thread'] != thread:
                return 'Interview answer rejected: wrong owner or conversation.'
            if rec.get('busy_until', 0) > time.time():
                return 'Interview is busy; try again.'
            pending = rec.get('pending')
            if not pending or not message_id or pending['nonce'] != nonce or pending['message_id'] != message_id or rec['phase'] not in {'awaiting_answer','awaiting_plan_decision'}:
                return 'This interview question is expired or already answered.'
            if pending['kind'] == 'budget':
                if token not in {'0', '1', '2'}:
                    return 'Use the recovery buttons; Other and text never authorize planning or execution.'
                rec = self.store.update(key, rec['revision'], pending=None,
                    phase='paused' if token == '2' else 'collecting')
                if token == '2':
                    return 'Interview paused. Findings retained; execution remains disabled. Use !interview resume or exit.'
                await self.advance(rec,
                    'Continue restricted research from the saved findings.' if token == '0' else
                    'Summarize what is known, distinguishing unresolved items and incomplete research.',
                    intent='collect' if token == '0' else 'summary')
                return 'Recovery choice recorded.'
            if pending['kind'] == 'project_choice':
                if token not in {str(i) for i in range(len(pending['choices']))}:
                    return 'Use the project selection buttons; Other and text never grant access.'
                from gateway.interview_permissions import validate_read_root
                try:
                    path = validate_read_root(pending['choices'][int(token)])
                except ValueError as exc:
                    return str(exc)
                await self.ask_read_permission(rec, path, 'Owner selected this project directory.')
                return 'Project selected. Read access still requires explicit approval.'
            if pending['kind'] == 'read_permission':
                if token not in {'0', '1'}:
                    return 'Use the owner-bound Approve or Deny read access buttons; text never grants access.'
                roots = rec.get('read_roots', [])
                if token == '0':
                    from gateway.interview_permissions import validate_read_root
                    try:
                        root = validate_read_root(pending.get('path'))
                    except ValueError as exc:
                        return str(exc)
                    roots = list(dict.fromkeys(roots + [root]))
                rec = self.store.update(key, rec['revision'], pending=None, phase='collecting', read_roots=roots)
                decision = 'approved' if token == '0' else 'denied'
                await self.advance(rec, f"Owner {decision} read access to {pending['path']}. Continue clarification without executing tasks.")
                return f'Read access {decision}.'
            if token == 'other':
                pending = dict(pending, awaiting_text=True)
                self.store.update(key, rec['revision'], pending=pending)
                return 'Type your answer in this thread. For planning, use !interview resume to revise requirements; Other never authorizes planning.'
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
        rec = self.store.update(rec['key'], rec['revision'], busy_until=time.time() + 300,
            phase='planning' if intent == 'plan' else 'collecting')
        try:
            async with asyncio.timeout(240):
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
        if result['kind'] == 'budget':
            rec = self.store.update(rec['key'], rec['revision'],
                budget={'reason': result['reason'], 'diagnostics': result['diagnostics']})
            await self.ask(rec,
                'Research reached its iteration or time budget. Findings are saved. '
                'Choose how to continue; task execution remains disabled.',
                ['Continue research', 'Summarize what is known', 'Stop here'], kind='budget')
        elif result['kind'] == 'permission':
            from gateway.interview_permissions import validate_read_root
            try:
                path = validate_read_root(result.get('path'))
                reason = result.get('reason')
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 32_768:
                    raise ValueError('Invalid interview read permission reason')
            except ValueError:
                self.store.update(rec['key'], rec['revision'], phase='paused', pending=None)
                await self.adapter().send_interview_text(chat_id=rec['source']['channel'],
                    content='Interview paused: the model supplied an invalid read permission request. '
                    'Saved research is retained; no read access, planning, or execution was authorized. '
                    'Use !interview approve-read /absolute/project (or a project name), '
                    'then !interview resume, or !interview finish to summarize what is known.',
                    metadata=self.metadata(rec))
                return
            await self.ask_read_permission(rec, path, f'Model-provided reason: {reason}')
        elif result['kind'] == 'question':
            await self.ask(rec, result['question'], result['choices'])
        else:
            is_plan = intent == 'plan'
            text = result['text']
            rec = self.store.update(rec['key'], rec['revision'],
                delivery={'kind': 'plan' if is_plan else 'summary', 'text': text},
                **({'plan': text, 'phase': 'plan_complete'} if is_plan else {'summary': text, 'phase': 'complete'}))
            await self.deliver_output(rec)

    async def ask_read_permission(self, rec, path, reason):
        await self.ask(rec,
            f"Read permission for interview owner {rec['owner']} only.\n"
            f"Project directory: {path}\n{reason}\n"
            'Approve bounded read-only access? This does not authorize execution.',
            ['Approve read access', 'Deny read access'], kind='read_permission', path=path)

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

    async def ask(self, rec, question, choices, kind='answer', *, path=None):
        pending = {'nonce': uuid.uuid4().hex, 'question': question, 'choices': choices,
                   'kind': kind, 'message_id': '', 'awaiting_text': not bool(choices)}
        if kind == 'read_permission':
            from gateway.interview_permissions import validate_read_root
            pending['path'] = validate_read_root(path)
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
