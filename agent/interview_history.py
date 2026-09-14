"""Scoped, read-only history for interviews; never call global session search.

Provenance contract comes from hermes_state_common.SCHEMA_SQL and
SessionDB.record_gateway_session_peer in hermes_state.py: sessions has source,
user_id, chat_id, chat_type, thread_id, origin_json and profile_name. Gateway
SessionStore._record_gateway_session_peer stores SessionSource.to_dict() in
origin_json; Slack's team is scope_id (legacy guild_id), NOT a guessed team_id
or a parsed session key. Both structured columns and origin must agree.

Only same-owner, same-team/channel Slack history is supported. Cross-thread
access requires an explicit session grant. The controller must authenticate the
requesting owner before calling validate_history_session(record, id), then
persist the returned ID in record['history_sessions'] with its normal revision
check. Validation never mutates the record or DB; each search reauthorizes.
Missing/contradictory provenance fails closed. Profile labels, when present,
must match the active profile; unlabeled legacy rows are confined by the
profile-local DB and still need complete Slack provenance.
"""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import stat
import time

from agent.redact import redact_sensitive_text
from hermes_constants import get_hermes_home
from hermes_cli.profiles import get_active_profile_name

_DENIED = 'Denied: history unavailable or outside the approved interview scope.'
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,255}\Z')
MAX_SESSIONS = 8
MAX_SCAN_MESSAGES = 200
MAX_INPUT_CHARS = 32_768
MAX_MESSAGE_CHARS = 2048
MAX_RESULT_CHARS = 16_000
MAX_MESSAGES = 50
MAX_QUERY_CHARS = 512
# Same conservative assignment/prefix policy as interview_policy._read_fd.
# Also use the shared forced redactor below. Suppress credential messages
# rather than echo token fragments or search raw secret values.
_CREDENTIAL = re.compile(
    r'(?i)-----BEGIN [^\n]*PRIVATE KEY-----|'
    r'(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|client[_-]?secret)'
    r'[\"\s]*[:=]|\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|'
    r'xox[baprs]-[A-Za-z0-9-]+)')


def _session_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(_DENIED)
    return value


def _connect():
    # Do not instantiate SessionDB: its constructor performs schema/FTS writes.
    path = Path(get_hermes_home()).resolve() / 'state.db'
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(_DENIED)
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=1)
    try:
        after = path.lstat()
        if (after.st_dev, after.st_ino, after.st_mode, after.st_nlink) != (
                info.st_dev, info.st_ino, info.st_mode, info.st_nlink):
            raise ValueError(_DENIED)
        conn.row_factory = sqlite3.Row
        deadline = time.monotonic() + 2.0
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.execute('PRAGMA query_only=ON')
        conn.execute('PRAGMA trusted_schema=OFF')
        conn.execute('BEGIN')  # Authorization and message reads share one snapshot.
        return conn
    except BaseException:
        conn.close()
        raise


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_DENIED)
        result[key] = value
    return result


def _authorize(conn, record, session_id):
    source = record.get('source')
    if not isinstance(source, dict):
        raise ValueError(_DENIED)
    owner = record.get('owner')
    if any(not isinstance(v, str) or not v.strip() for v in
           (owner, source.get('team'), source.get('channel'), source.get('thread'))):
        raise ValueError(_DENIED)
    profile = get_active_profile_name()
    if source.get('profile') not in (None, '', profile):
        raise ValueError(_DENIED)
    row = conn.execute(
        'SELECT source, user_id, chat_id, chat_type, thread_id, origin_json, '
        'profile_name FROM sessions WHERE id = ?', (session_id,)).fetchone()
    if row is None or not isinstance(row['origin_json'], str):
        raise ValueError(_DENIED)
    if len(row['origin_json']) > MAX_INPUT_CHARS:
        raise ValueError(_DENIED)
    origin = json.loads(row['origin_json'], object_pairs_hook=_unique_object)
    if not isinstance(origin, dict):
        raise ValueError(_DENIED)
    expected = {'user_id': owner, 'chat_id': source['channel']}
    # The historical thread need not be the interview thread, but both stored
    # provenance representations must name the same nonempty thread. Search
    # still requires an explicit history_sessions grant before authorization.
    if (not isinstance(row['thread_id'], str) or not row['thread_id'].strip()
            or row['thread_id'] != origin.get('thread_id')):
        raise ValueError(_DENIED)
    if (row['source'] != 'slack' or origin.get('platform') != 'slack'
            or row['chat_type'] not in ('channel', 'group', 'thread')
            or origin.get('chat_type') != row['chat_type']
            or row['profile_name'] not in (None, '', profile)
            or origin.get('profile') not in (None, '', profile)):
        raise ValueError(_DENIED)
    if any(row[key] != value or origin.get(key) != value for key, value in expected.items()):
        raise ValueError(_DENIED)
    # Legacy alias is accepted only if canonical scope_id is absent, not null;
    # a conflicting dual-write is corruption, never a reason to choose a side.
    if origin.get('scope_id', origin.get('guild_id')) != source['team']:
        raise ValueError(_DENIED)
    if 'guild_id' in origin and origin['guild_id'] != source['team']:
        raise ValueError(_DENIED)


def validate_history_session(record, session_id) -> str:
    """Return a validated bare ID or raise ValueError; read-only, no grant write.

    Call ONLY after controller owner authentication. No source/profile override
    parameters exist. The returned ID must be explicitly persisted by the
    controller before history_search will expose that session's messages.
    """
    try:
        _session_id(session_id)
        with closing(_connect()) as conn:
            _authorize(conn, record, session_id)
        return session_id
    except (ValueError, TypeError, AttributeError, RecursionError, OSError, sqlite3.Error) as exc:
        raise ValueError(_DENIED) from exc


def history_search(record, query='', session_id=None) -> dict:
    """Literal case-insensitive search; None includes current + approved IDs.

    An explicit session_id restricts results to that approved session. Returns
    {messages: [{session_id: str|None, role, content}], truncated: bool}; errors
    return {error: str}, without partial messages or existence/path disclosures.
    Searches only the latest 200 eligible messages per source, in chronological
    order (current interview first). At most eight approved sessions, 50 result
    messages, 2048 characters per result and 16000 total content characters.
    Queries are at most 512 characters; messages over 32768 characters and
    recognizable credential/multimodal/tool payloads are omitted, not clipped
    before security filtering. ``truncated`` signals scan/output limits, not
    credential suppression. This is bounded retrieval, not an exhaustive index.
    """
    try:
        if not isinstance(query, str) or len(query) > MAX_QUERY_CHARS:
            raise ValueError(_DENIED)
        grants = record.get('history_sessions', [])
        if not isinstance(grants, list) or len(grants) > MAX_SESSIONS:
            raise ValueError(_DENIED)
        ids = list(dict.fromkeys(_session_id(sid) for sid in grants))
        if session_id is not None:
            _session_id(session_id)
            if session_id not in ids:
                raise ValueError(_DENIED)
            ids = [session_id]
        candidates = []
        truncated = False
        if session_id is None:
            current = record.get('messages', [])
            if not isinstance(current, list):
                raise ValueError(_DENIED)
            truncated = len(current) > MAX_SCAN_MESSAGES
            candidates.extend((None, m) for m in current[-MAX_SCAN_MESSAGES:])
        if ids:
            with closing(_connect()) as conn:
                for sid in ids:
                    _authorize(conn, record, sid)
                for sid in ids:
                    rows = conn.execute(
                        "SELECT role, substr(content, 1, ?) AS content "
                        "FROM messages WHERE session_id = ? "
                        "AND role IN ('user', 'assistant') AND typeof(content) = 'text' "
                        "AND (tool_calls IS NULL OR tool_calls IN ('', '[]', 'null')) "
                        "AND tool_call_id IS NULL AND tool_name IS NULL "
                        "ORDER BY id DESC LIMIT ?",
                        (MAX_INPUT_CHARS + 1, sid, MAX_SCAN_MESSAGES + 1)).fetchall()
                    truncated |= len(rows) > MAX_SCAN_MESSAGES
                    candidates.extend((sid, dict(row)) for row in reversed(rows[:MAX_SCAN_MESSAGES]))
        messages = []
        remaining = MAX_RESULT_CHARS
        needle = query.casefold()
        for sid, message in candidates:
            if (not isinstance(message, dict) or message.get('role') not in ('user', 'assistant')
                    or message.get('tool_calls') or message.get('tool_call_id')
                    or message.get('tool_name')):
                continue
            text = message.get('content')
            if not isinstance(text, str) or not text or '\x00' in text:
                continue  # Includes SessionDB's NUL-prefixed multimodal sentinel.
            if len(text) > MAX_INPUT_CHARS:
                truncated = True
                continue  # Never clip a credential before examining it.
            if _CREDENTIAL.search(text):
                continue
            safe = redact_sensitive_text(text, force=True, redact_url_credentials=True)
            if safe != text:
                continue
            if needle not in safe.casefold():
                continue
            if len(messages) >= MAX_MESSAGES or remaining == 0:
                truncated = True
                break
            content = safe[:min(MAX_MESSAGE_CHARS, remaining)]
            truncated |= len(content) < len(safe)
            remaining -= len(content)
            messages.append({'session_id': sid, 'role': message['role'], 'content': content})
        return {'messages': messages, 'truncated': truncated}
    except (ValueError, TypeError, AttributeError, RecursionError, OSError, sqlite3.Error):
        return {'error': _DENIED}
