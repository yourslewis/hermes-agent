"""Profile-local durable interview state, independent of conversation storage."""

from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
from uuid import UUID, uuid4


PHASES = frozenset({
    "collecting", "awaiting_answer", "summarizing", "complete", "paused", "cancelled",
    "awaiting_plan_decision", "planning", "plan_complete", "exited",
})
IMMUTABLE_FIELDS = frozenset({"id", "key", "owner", "task", "bank", "source", "revision"})


def _validate_json(value):
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _validate_json(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _validate_json(item)
        return
    raise ValueError("Invalid interview JSON value")


def _validate_state(state):
    for field in ("id", "key", "owner", "task"):
        if type(state.get(field)) is not str or not state[field].strip():
            raise ValueError(f"Invalid interview {field}")
    revision = state.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("Invalid interview revision")
    if type(state.get("phase")) is not str or state["phase"] not in PHASES:
        raise ValueError("Invalid interview phase")
    for field in ("bank", "source"):
        if type(state.get(field)) is not dict:
            raise ValueError(f"Invalid interview {field}")
    for field in ("team", "channel", "thread"):
        value = state["source"].get(field)
        if type(value) is not str or not value.strip():
            raise ValueError(f"Invalid interview source {field}")
    for field in ("messages", "answers"):
        if type(state.get(field)) is not list:
            raise ValueError(f"Invalid interview {field}")
    if "pending" not in state or (state["pending"] is not None and type(state["pending"]) is not dict):
        raise ValueError("Invalid interview pending")
    if state["phase"] == "exited" and state["pending"] is not None:
        raise ValueError("Invalid interview exited pending action")
    for field in ("summary", "plan"):
        if type(state.get(field)) is not str:
            raise ValueError(f"Invalid interview {field}")


def _encode(state):
    try:
        _validate_state(state)
        _validate_json(state)
        return json.dumps(state, allow_nan=False)
    except (TypeError, RecursionError, OverflowError) as exc:
        raise ValueError("Invalid interview JSON") from exc


def _validate_key(key):
    if type(key) is not str or not key.strip():
        raise ValueError("Invalid interview key")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _decode(row, key):
    try:
        revision, encoded = row
        if type(encoded) is not str or type(revision) is not int:
            raise ValueError("Invalid SQLite row types")
        state = json.loads(encoded, object_pairs_hook=_unique_object)
        if type(state) is not dict:
            raise ValueError("State is not an object")
        _validate_state(state)
        _validate_json(state)
        if str(UUID(state["id"])) != state["id"]:
            raise ValueError("Invalid interview UUID")
        if state["key"] != key or state["revision"] != revision:
            raise ValueError("Snapshot does not match SQLite row")
        return state
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError("Corrupt interview state; refusing to continue") from exc


class InterviewStore:
    """Durable snapshots in an explicit profile-local SQLite file.

    The caller resolves the profile path (typically HERMES_HOME/interviews.sqlite3)
    and enforces owner/source authorization and phase transitions. This store only
    permits known phases and never implicitly deletes or exits a record. Extra
    fields may contain strict JSON values; identity, source, task and bank are
    immutable. Pending question/answer/message schemas belong to the controller.

    Each operation opens and closes its own connection, so a store can be shared
    across worker threads without a shared SQLite connection. Writes acquire a
    database transaction before reading; revisions also participate in the SQL
    update predicate. Revisions continue increasing after exit/recreate, avoiding
    stale-revision ABA writes. There is no resource-level close() requirement.

    Invalid input, corruption, revision conflicts, duplicate entry and absent
    updates raise descriptive ValueError. SQLite I/O/schema errors propagate;
    callers must fail closed rather than fall back to unrestricted routing.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS interviews ("
                "key TEXT PRIMARY KEY NOT NULL, revision INTEGER NOT NULL, "
                "state TEXT NOT NULL)"
            )

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def get(self, key: str) -> dict | None:
        _validate_key(key)
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT revision, state FROM interviews WHERE key = ?", (key,)).fetchone()
            return _decode(row, key) if row else None

    def create(self, key: str, owner: str, task: str, bank: dict, source: dict) -> dict:
        _validate_key(key)
        state = {
            "id": str(uuid4()), "key": key, "owner": owner, "task": task,
            "bank": bank, "source": source, "revision": 0, "phase": "collecting",
            "messages": [], "answers": [], "pending": None, "summary": "", "plan": "",
        }
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision, state FROM interviews WHERE key = ?", (key,)).fetchone()
            if row is not None:
                previous = _decode(row, key)
                if previous["phase"] != "exited":
                    raise ValueError("Interview already exists; explicit exit required")
                state["revision"] = previous["revision"] + 1
            encoded = _encode(state)
            conn.execute(
                "INSERT INTO interviews VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET revision=excluded.revision, state=excluded.state",
                (key, state["revision"], encoded),
            )
        return json.loads(encoded)

    def exit(self, key: str, expected_revision: int) -> dict:
        """Persist an explicit exit tombstone; authorization belongs to the controller."""
        return self.update(key, expected_revision, phase="exited", pending=None)

    def update(self, key: str, expected_revision: int, **fields) -> dict:
        _validate_key(key)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Invalid interview expected revision")
        if IMMUTABLE_FIELDS.intersection(fields):
            raise ValueError("Invalid interview update: immutable fields")
        with closing(self._connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT revision, state FROM interviews WHERE key = ?", (key,)).fetchone()
            if row is None:
                raise ValueError("Interview not found")
            state = _decode(row, key)
            if state["revision"] != expected_revision:
                raise ValueError("Interview revision conflict")
            if state["phase"] == "exited":
                raise ValueError("Interview exited; create a new interview")
            state.update(fields)
            if state["phase"] == "exited":
                state["pending"] = None
            state["revision"] = expected_revision + 1
            encoded = _encode(state)
            result = conn.execute(
                "UPDATE interviews SET revision = ?, state = ? WHERE key = ? AND revision = ?",
                (state["revision"], encoded, key, expected_revision),
            )
            if result.rowcount != 1:
                raise ValueError("Interview revision conflict")
        return json.loads(encoded)
