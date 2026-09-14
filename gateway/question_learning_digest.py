"""Daily batching on the owner's next admitted message; no synthetic chat polling."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import time

_locks = {}

async def deliver_digest(adapter, home, source, now=None):
    home = Path(home)
    now = time.time() if now is None else now
    lock = _locks.setdefault(str(home), asyncio.Lock())
    async with lock:
        try:
            from agent.interview_questions import _directory_path, _read
            with _directory_path(home) as fd:
                raw, _ = _read('question-learning.json', fd)
            config = json.loads(raw)
            if (not isinstance(config, dict) or config.get('profile') != 'rex'
                    or config.get('owner') != source.user_id or config.get('team') != source.scope_id
                    or source.chat_id not in config.get('channels', [])):
                return
            path = home / 'question-learning.sqlite3'
            if not path.is_file() or path.is_symlink():
                return
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE IF NOT EXISTS digest_delivery (id INTEGER PRIMARY KEY, sent REAL NOT NULL)')
                previous = db.execute('SELECT sent FROM digest_delivery WHERE id=1').fetchone()
                rows = db.execute("SELECT event_id,state,question_id,created FROM events WHERE delivered=0 "
                    "AND state IN ('added','proposed','tombstoned','conflict') ORDER BY created,event_id LIMIT 20").fetchall()
                if not rows or now - (previous[0] if previous else rows[0][3]) < 86400:
                    return
            text = 'Question Bank changes (auto-generated):\n' + '\n'.join(
                '- ' + state + ': ' + (qid or 'unknown') for _,state,qid,_ in rows)
            text += '\nReview in Notes → Question Bank. Undo: !interview learning undo QUESTION_ID'
            result = await adapter.send(source.chat_id, text, thread_id=source.thread_id)
            if not result.success:
                return
            # Only acknowledge the exact batch that was delivered; changes arriving
            # during network I/O stay pending. A crash before this commit may repeat
            # the digest, but cannot silently lose changes.
            with sqlite3.connect(path) as db:
                db.executemany('UPDATE events SET delivered=1 WHERE event_id=? AND state=? AND question_id IS ? AND created=?', rows)
                db.execute('INSERT OR REPLACE INTO digest_delivery(id,sent) VALUES(1,?)',(now,))
        except (OSError, ValueError, TypeError, sqlite3.Error):
            return
