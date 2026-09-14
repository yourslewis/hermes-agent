"""Daily change batching uses delivery acknowledgement, not destructive previews."""
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest

@pytest.mark.asyncio
async def test_digest_waits_batches_and_acknowledges_only_success(tmp_path):
    from gateway.question_learning_digest import deliver_digest
    home = tmp_path / 'rex'; home.mkdir()
    (home / 'question-learning.json').write_text(json.dumps({'enabled':True, 'profile':'rex','owner':'U','team':'T','channels':['C']}))
    with sqlite3.connect(home / 'question-learning.sqlite3') as db:
        db.execute('CREATE TABLE events(event_id TEXT PRIMARY KEY,state TEXT,question_id TEXT,created REAL,delivered INTEGER)')
        db.execute("INSERT INTO events VALUES('event','added','learned-test',1,0)")
    adapter = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(success=False)))
    source = SimpleNamespace(user_id='U',scope_id='T',chat_id='C',thread_id='th')
    await deliver_digest(adapter,home,source,now=2)
    adapter.send.assert_not_awaited()
    await deliver_digest(adapter,home,source,now=90000)
    assert adapter.send.await_count == 1
    with sqlite3.connect(home / 'question-learning.sqlite3') as db:
        assert db.execute('SELECT delivered FROM events').fetchone()[0] == 0
    adapter.send.return_value.success = True
    await deliver_digest(adapter,home,source,now=90001)
    with sqlite3.connect(home / 'question-learning.sqlite3') as db:
        assert db.execute('SELECT delivered FROM events').fetchone()[0] == 1
    await deliver_digest(adapter,home,source,now=200000)
    assert adapter.send.await_count == 2

@pytest.mark.asyncio
async def test_digest_does_not_acknowledge_undo_arriving_during_send(tmp_path):
    from gateway.question_learning_digest import deliver_digest
    home=tmp_path/'rex'; home.mkdir()
    (home/'question-learning.json').write_text(json.dumps({'profile':'rex','owner':'U','team':'T','channels':['C']}))
    with sqlite3.connect(home/'question-learning.sqlite3') as db:
        db.execute('CREATE TABLE events(event_id TEXT,state TEXT,question_id TEXT,created REAL,delivered INTEGER)')
        db.execute("INSERT INTO events VALUES('event','added','q',1,0)")
    async def send(*args,**kwargs):
        with sqlite3.connect(home/'question-learning.sqlite3') as db:
            db.execute("UPDATE events SET state='tombstoned',delivered=0")
        return SimpleNamespace(success=True)
    await deliver_digest(SimpleNamespace(send=send),home,SimpleNamespace(user_id='U',scope_id='T',chat_id='C',thread_id='th'),now=90000)
    with sqlite3.connect(home/'question-learning.sqlite3') as db:
        assert db.execute('SELECT delivered FROM events').fetchone()[0]==0

@pytest.mark.asyncio
async def test_digest_never_delivers_to_other_author(tmp_path):
    from gateway.question_learning_digest import deliver_digest
    adapter = SimpleNamespace(send=AsyncMock())
    await deliver_digest(adapter,tmp_path,SimpleNamespace(user_id='other',scope_id='T',chat_id='C',thread_id='th'),now=100000)
    adapter.send.assert_not_awaited()
