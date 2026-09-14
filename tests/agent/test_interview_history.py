"""Security contract for interview-only, read-only history retrieval."""
import importlib
import json
import sqlite3
from contextlib import closing

import pytest


@pytest.fixture
def stored(tmp_path, monkeypatch):
    from agent import interview_history as history
    from hermes_state_common import SCHEMA_SQL
    monkeypatch.setattr(history, 'get_hermes_home', lambda: tmp_path, raising=False)
    monkeypatch.setattr(history, 'get_active_profile_name', lambda: 'test-profile', raising=False)
    path = tmp_path / 'state.db'
    origin = {'platform': 'slack', 'scope_id': 'T1', 'guild_id': 'T1',
              'chat_id': 'C1', 'chat_type': 'channel', 'thread_id': '123.456',
              'user_id': 'U1', 'profile': 'test-profile'}
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript(SCHEMA_SQL)
        conn.execute('INSERT INTO sessions (id, source, user_id, chat_id, chat_type, '
                     'thread_id, origin_json, profile_name, started_at) '
                     'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                     ('allowed', 'slack', 'U1', 'C1', 'channel', '123.456',
                      json.dumps(origin), 'test-profile', 1))
        for role, text in [('user', 'Historic blue requirement'),
                           ('assistant', 'Historic blue answer'),
                           ('tool', 'SECRET TOOL'), ('system', 'SECRET SYSTEM')]:
            conn.execute('INSERT INTO messages (session_id, role, content, timestamp) '
                         'VALUES (?, ?, ?, ?)', ('allowed', role, text, 1))
    record = {'owner': 'U1', 'source': {'team': 'T1', 'channel': 'C1',
              'thread': '123.456', 'profile': 'test-profile'},
              'history_sessions': ['allowed'],
              'messages': [{'role': 'user', 'content': 'Current blue question'}]}
    return history, path, record, origin


def test_explicit_id_reads_only_granted_session(stored):
    history, path, record, _ = stored
    assert history.validate_history_session(record, 'allowed') == 'allowed'
    result = history.history_search(record, 'BLUE', 'allowed')
    assert [m['content'] for m in result['messages']] == [
        'Historic blue requirement', 'Historic blue answer']
    assert all(m['session_id'] == 'allowed' for m in result['messages'])
    assert len(history.history_search(record, 'blue')['messages']) == 3
    record['history_sessions'] = []
    assert 'error' in history.history_search(record, session_id='allowed')
    assert history.validate_history_session(record, 'allowed') == 'allowed'
    assert len(history.history_search(record)['messages']) == 1


def test_cross_thread_history_requires_explicit_grant(stored):
    history, path, record, _ = stored
    record['source']['thread'] = 'different-interview-thread'
    record['history_sessions'] = []
    before = path.read_bytes()

    validated = history.validate_history_session(record, 'allowed')
    assert validated == 'allowed'
    assert record['history_sessions'] == []
    assert path.read_bytes() == before
    assert 'error' in history.history_search(record, session_id=validated)
    assert history.history_search(record)['messages'] == [
        {'session_id': None, 'role': 'user', 'content': 'Current blue question'}]

    record['history_sessions'].append(validated)
    assert [m['content'] for m in history.history_search(
        record, session_id=validated)['messages']] == [
            'Historic blue requirement', 'Historic blue answer']
    record['history_sessions'].clear()
    assert 'error' in history.history_search(record, session_id=validated)


@pytest.mark.parametrize('column,value', [
    ('source', 'cli'), ('source', 'telegram'), ('user_id', 'U2'),
    ('user_id', None), ('chat_id', 'C2'), ('chat_id', None),
    ('chat_type', 'dm'), ('thread_id', 'other-thread'), ('thread_id', None),
    ('profile_name', 'other-profile'), ('origin_json', None),
    ('origin_json', '{}'), ('origin_json', 'not JSON'),
])
def test_stored_provenance_denials(stored, column, value):
    history, path, record, _ = stored
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(f'UPDATE sessions SET {column} = ? WHERE id = ?', (value, 'allowed'))
    with pytest.raises(ValueError):
        history.validate_history_session(record, 'allowed')
    assert 'error' in history.history_search(record, session_id='allowed')


@pytest.mark.parametrize('field,value', [
    ('platform', 'cli'), ('scope_id', 'T2'), ('scope_id', None),
    ('guild_id', 'T2'), ('chat_id', 'C2'), ('chat_id', None),
    ('chat_type', 'dm'), ('user_id', 'U2'), ('user_id', None),
    ('thread_id', 'other'), ('thread_id', None), ('profile', 'other-profile'),
])
def test_origin_json_denials(stored, field, value):
    history, path, record, origin = stored
    origin[field] = value
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('UPDATE sessions SET origin_json = ?', (json.dumps(origin),))
    assert 'error' in history.history_search(record)


@pytest.mark.parametrize('field,value', [
    ('owner', 'U2'), ('owner', None), ('team', 'T2'), ('team', ''),
    ('channel', 'C2'), ('thread', ''), ('thread', None), ('profile', 'other-profile'),
])
def test_requesting_record_denials(stored, field, value):
    history, _, record, _ = stored
    (record if field == 'owner' else record['source'])[field] = value
    assert 'error' in history.history_search(record)


@pytest.mark.parametrize('session_id', ["allowed' OR 1=1 --", '../other/state.db',
    'other/allowed', '@session:other/allowed', 'file:other?mode=rw', '', None, 42, 'a' * 257])
def test_malicious_grant_ids_denied(stored, session_id):
    history, _, record, _ = stored
    with pytest.raises(ValueError):
        history.validate_history_session(record, session_id)


def test_sqlite_reads_are_readonly_scoped_and_do_not_write(stored, monkeypatch):
    history, path, record, _ = stored
    original = sqlite3.connect
    queries = []
    before = path.read_bytes()

    def connect(database, *args, **kwargs):
        assert database == path.as_uri() + '?mode=ro'
        assert kwargs['uri'] is True
        conn = original(database, *args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute('CREATE TABLE should_never_exist (x)')
        conn.set_trace_callback(queries.append)
        return conn

    monkeypatch.setattr(history.sqlite3, 'connect', connect)
    history.validate_history_session(record, 'allowed')
    assert 'error' not in history.history_search(record)
    assert path.read_bytes() == before
    selects = [q for q in queries if q.startswith('SELECT')]
    assert selects
    assert all("= 'allowed'" in q for q in selects)
    assert not any('MATCH' in q or 'messages_fts' in q for q in queries)


def test_missing_db_is_not_created(tmp_path, monkeypatch):
    from agent import interview_history as history
    monkeypatch.setattr(history, 'get_hermes_home', lambda: tmp_path)
    with pytest.raises(ValueError):
        history.validate_history_session({}, 'missing')
    assert not (tmp_path / 'state.db').exists()


def test_bounds_and_credentials_for_current_messages(monkeypatch):
    from agent import interview_history as history
    monkeypatch.setenv('HERMES_REDACT_SECRETS', 'false')
    secret = 'sk-' + 'A' * 40
    record = {'messages': [
        {'role': 'user', 'content': f'api_key={secret}'},
        {'role': 'user', 'content': 'password: hunter2'},
        {'role': 'assistant', 'content': 'Hidden tool invocation', 'tool_calls': [{}]},
        {'role': 'user', 'content': [{'type': 'image', 'data': 'PRIVATE'}]},
        {'role': 'assistant', 'content': 'x' * 100_000},
    ] + [{'role': 'user', 'content': 'safe ' + 'x' * 3000} for _ in range(300)]}
    result = history.history_search(record)
    assert result['truncated'] is True
    assert len(result['messages']) <= 50
    assert sum(len(m['content']) for m in result['messages']) <= 16_000
    assert all(len(m['content']) <= 2048 for m in result['messages'])
    serialized = json.dumps(result)
    assert secret not in serialized and 'hunter2' not in serialized
    assert 'Hidden tool invocation' not in serialized and 'PRIVATE' not in serialized
    assert 'error' in history.history_search(record, 'x' * 513)
    assert 'error' in history.history_search(record, 2)


def test_stored_bounds_credentials_and_recent_window(stored):
    history, path, record, _ = stored
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM messages WHERE session_id = 'allowed'")
        conn.execute("INSERT INTO messages(session_id, role, content, timestamp) "
                     "VALUES ('allowed', 'user', 'outside-window', 1)")
        for index in range(250):
            conn.execute("INSERT INTO messages(session_id, role, content, timestamp) "
                         "VALUES ('allowed', 'user', ?, 1)", ('safe ' + str(index) + 'x' * 3000,))
        conn.execute("INSERT INTO messages(session_id, role, content, timestamp, tool_calls) "
                     "VALUES ('allowed', 'assistant', 'tool-call-content', 1, '[{}]')")
        conn.execute("INSERT INTO messages(session_id, role, content, timestamp) "
                     "VALUES ('allowed', 'user', 'client_secret=superprivate', 1)")
    result = history.history_search(record, session_id='allowed')
    assert result['truncated'] is True
    assert len(result['messages']) <= 50
    assert sum(len(m['content']) for m in result['messages']) <= 16_000
    assert 'superprivate' not in json.dumps(result)
    assert 'tool-call-content' not in json.dumps(result)
    assert history.history_search(record, 'outside-window', 'allowed')['messages'] == []


def test_symlinked_other_profile_db_denied(stored, tmp_path):
    history, path, record, _ = stored
    other = tmp_path / 'other-profile.db'
    path.rename(other)
    try:
        path.symlink_to(other)
    except OSError:
        pytest.skip('Symlink creation unavailable')
    assert 'error' in history.history_search(record)


def test_too_many_grants_denied(stored):
    history, _, record, _ = stored
    record['history_sessions'] = ['allowed'] * 9
    assert 'error' in history.history_search(record)


@pytest.mark.parametrize('text', [
    'api_key=sk-' + 'A' * 40, 'password: hunter2', 'client_secret=superprivate',
    'https://example.test/?access_token=superprivate',
    '-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----',
    '\x00json:[{"type":"image_url","image_url":"private"}]',
])
def test_sensitive_text_never_returned_or_searchable(stored, monkeypatch, text):
    from agent import redact
    history, path, record, _ = stored
    monkeypatch.setattr(redact, '_REDACT_ENABLED', False)
    record['messages'] = [{'role': 'user', 'content': text}]
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('DELETE FROM messages')
        conn.execute('INSERT INTO messages(session_id, role, content, timestamp) '
                     'VALUES (?, ?, ?, ?)', ('allowed', 'user', text, 1))
    assert history.history_search(record)['messages'] == []
    assert history.history_search(record, 'secret')['messages'] == []


def test_legacy_team_alias_and_unlabeled_local_profile(stored):
    history, path, record, origin = stored
    del origin['scope_id']
    del origin['profile']
    record['source']['profile'] = ''
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('UPDATE sessions SET origin_json = ?, profile_name = NULL',
                     (json.dumps(origin),))
    assert history.validate_history_session(record, 'allowed') == 'allowed'


def test_not_found_same_denial_as_forbidden_and_no_cross_profile_fallback(stored, tmp_path):
    history, path, record, _ = stored
    other = tmp_path / 'profiles' / 'other'
    other.mkdir(parents=True)
    (other / 'state.db').write_bytes(path.read_bytes())
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('DELETE FROM messages')
        conn.execute('DELETE FROM sessions')
    result = history.history_search(record, session_id='allowed')
    assert 'error' in result
    assert result == history.history_search(record, session_id='ungranted')


def test_deeply_nested_origin_fails_closed(stored):
    history, path, record, _ = stored
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute('UPDATE sessions SET origin_json = ?', ('[' * 2000 + ']' * 2000,))
    assert 'error' in history.history_search(record)
    with pytest.raises(ValueError):
        history.validate_history_session(record, 'allowed')


def test_current_context_search_is_literal_and_needs_no_database(tmp_path, monkeypatch):
    monkeypatch.setenv('HERMES_HOME', str(tmp_path / 'absent'))
    history = importlib.import_module('agent.interview_history')
    record = {'messages': [
        {'role': 'user', 'content': 'Need a BLUE widget'},
        {'role': 'assistant', 'content': 'Blue or green?'},
        {'role': 'system', 'content': 'blue hidden instruction'},
        {'role': 'tool', 'content': 'blue tool payload'},
    ]}
    result = history.history_search(record, 'blue')
    assert [m['content'] for m in result['messages']] == ['Need a BLUE widget', 'Blue or green?']
    assert all(m['session_id'] is None for m in result['messages'])
    assert history.history_search(record, '%')['messages'] == []
    assert not (tmp_path / 'absent').exists()
