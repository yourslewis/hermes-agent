"""Durable, fail-closed interview storage; no live gateway dependencies."""

import importlib
import importlib.util
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

import pytest


KEY = "slack:T1:C1:123.456"
SOURCE = {"team": "T1", "channel": "C1", "thread": "123.456"}
BANK = {"version": "1", "questions": [{"id": "goal", "question": "Why?"}]}


def store_at(path):
    # A missing module is an explicit RED assertion rather than collection failure.
    assert importlib.util.find_spec("gateway.interview_store"), "InterviewStore missing"
    return importlib.import_module("gateway.interview_store").InterviewStore(path)


def create(store, key=KEY):
    return store.create(key, "U1", "Build a useful thing", BANK, SOURCE)


def test_create_get_roundtrip_survives_reopening(tmp_path):
    path = tmp_path / "profile" / "interviews.sqlite3"
    store = store_at(path)
    assert store.get(KEY) is None
    state = create(store)
    assert UUID(state["id"]).version == 4
    assert state == {
        "id": state["id"], "key": KEY, "owner": "U1",
        "task": "Build a useful thing", "bank": BANK, "source": SOURCE,
        "revision": 0, "phase": "collecting", "messages": [], "answers": [],
        "pending": None, "summary": "", "plan": "",
    }
    assert store.get(KEY) == state
    assert store_at(path).get(KEY) == state
    # Returned state must not alias the durable snapshot.
    state["bank"]["questions"].clear()
    assert store.get(KEY)["bank"] == BANK


def test_update_compare_and_swap_preserves_original_task(tmp_path):
    path = tmp_path / "interviews.sqlite3"
    store = store_at(path)
    initial = create(store)
    updated = store.update(KEY, initial["revision"], phase="awaiting_answer",
                           pending={"id": "q1", "choices": ["A", "B"]},
                           messages=[{"role": "assistant", "content": "Which?"}],
                           delivery={"message_id": "12.34"})
    assert updated["revision"] == 1
    assert updated["id"] == initial["id"]
    assert updated["task"] == initial["task"]
    assert updated["delivery"] == {"message_id": "12.34"}
    assert store_at(path).get(KEY) == updated
    with pytest.raises(ValueError, match="conflict"):
        store.update(KEY, 0, answers=["replayed"])
    assert store.get(KEY) == updated
    with pytest.raises(ValueError, match="not found"):
        store.update("missing", 0, summary="oops")


@pytest.mark.parametrize("phase", ["collecting", "complete", "plan_complete", "paused", "cancelled"])
def test_only_explicit_exit_allows_recreation(tmp_path, phase):
    path = tmp_path / "interviews.sqlite3"
    store = store_at(path)
    initial = create(store)
    active = store.update(KEY, 0, phase=phase, pending={"id": "q1"})
    with pytest.raises(ValueError, match="already exists"):
        create(store)
    assert store.get(KEY) == active
    exited = store.exit(KEY, active["revision"])
    assert exited["phase"] == "exited"
    assert exited["pending"] is None
    assert store_at(path).get(KEY) == exited
    with pytest.raises(ValueError, match="exited"):
        store.update(KEY, exited["revision"], phase="collecting")
    replacement = create(store)
    assert replacement["id"] != initial["id"]
    assert replacement["phase"] == "collecting"
    # Keep revisions monotonic across generations to prevent an ABA stale write.
    assert replacement["revision"] == exited["revision"] + 1
    with pytest.raises(ValueError, match="conflict"):
        store.update(KEY, initial["revision"], summary="old interview")


@pytest.mark.parametrize("fields", [
    {"phase": "execute"}, {"phase": None}, {"messages": {}}, {"answers": "answer"},
    {"pending": []}, {"summary": None}, {"plan": {}},
    {"owner": "U2"}, {"id": "replacement"}, {"task": "changed"},
    {"bank": {}}, {"source": SOURCE}, {"revision": 999},
    {"metadata": {"score": float("nan")}}, {"metadata": float("inf")},
    {"metadata": {1: "not a JSON key"}}, {"metadata": ("tuple",)},
    {"metadata": object()},
])
def test_invalid_update_fails_without_mutating_state(tmp_path, fields):
    store = store_at(tmp_path / "interviews.sqlite3")
    initial = create(store)
    with pytest.raises(ValueError, match="Invalid interview"):
        store.update(KEY, 0, **fields)
    assert store.get(KEY) == initial


@pytest.mark.parametrize("bad_revision", [True, False, -1, 0.0, "0", None])
def test_revision_must_be_nonnegative_integer(tmp_path, bad_revision):
    store = store_at(tmp_path / "interviews.sqlite3")
    initial = create(store)
    with pytest.raises(ValueError, match="revision"):
        store.update(KEY, bad_revision, summary="not accepted")
    assert store.get(KEY) == initial


@pytest.mark.parametrize("fields", [
    {"owner": ""}, {"task": None}, {"bank": []}, {"source": {}},
    {"source": {"team": "T1", "channel": "C1", "thread": ""}},
    {"source": {"team": 1, "channel": "C1", "thread": "123"}},
    {"bank": {"x": float("nan")}},
])
def test_invalid_creation_does_not_insert(tmp_path, fields):
    store = store_at(tmp_path / "interviews.sqlite3")
    args = {"owner": "U1", "task": "A task", "bank": BANK, "source": SOURCE}
    args.update(fields)
    with pytest.raises(ValueError, match="Invalid interview"):
        store.create(KEY, **args)
    assert store.get(KEY) is None


# Corruption tests intentionally bypass the store to simulate damaged persisted rows.
def corrupt(path, encoded, revision=0):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE interviews SET state = ?, revision = ? WHERE key = ?",
                     (encoded, revision, KEY))


@pytest.mark.parametrize("damage", [
    "broken_json", "array", "null", "duplicate_key", "nan", "overflow",
    "missing_owner", "missing_pending", "id", "key", "revision", "row_revision",
    "phase", "messages", "answers", "pending", "summary", "plan", "bank", "source",
])
def test_corrupt_records_fail_closed_on_read_update_create_exit(tmp_path, damage):
    path = tmp_path / "interviews.sqlite3"
    store = store_at(path)
    state = create(store)
    row_revision = 0
    encoded = None
    if damage == "broken_json":
        encoded = "{incomplete"
    elif damage == "array":
        encoded = "[]"
    elif damage == "null":
        encoded = "null"
    elif damage == "duplicate_key":
        encoded = json.dumps(state)[:-1] + ', "phase": "exited"}'
    elif damage == "nan":
        encoded = json.dumps(state)[:-1] + ', "metadata": NaN}'
    elif damage == "overflow":
        encoded = json.dumps(state)[:-1] + ', "metadata": 1e999}'
    elif damage.startswith("missing_"):
        del state[damage.removeprefix("missing_")]
    elif damage == "row_revision":
        row_revision = 99
    else:
        state[damage] = {
            "id": "not-a-uuid", "key": "wrong thread", "revision": True,
            "phase": "unrestricted", "messages": {}, "answers": {}, "pending": [],
            "summary": None, "plan": None, "bank": [], "source": {},
        }[damage]
    corrupt(path, encoded if encoded is not None else json.dumps(state), row_revision)
    reopened = store_at(path)
    for operation in (lambda: reopened.get(KEY), lambda: reopened.update(KEY, 0, phase="exited"),
                      lambda: create(reopened), lambda: reopened.exit(KEY, 0)):
        with pytest.raises(ValueError, match="Corrupt interview"):
            operation()


@pytest.mark.parametrize("key", [None, "", "  ", 42, b"thread"])
def test_invalid_keys_fail_closed(tmp_path, key):
    store = store_at(tmp_path / "interviews.sqlite3")
    for operation in (lambda: store.get(key), lambda: create(store, key),
                      lambda: store.update(key, 0, summary="no")):
        with pytest.raises(ValueError, match="Invalid interview key"):
            operation()


@pytest.mark.parametrize("shared_store", [True, False])
def test_atomic_updates_have_exactly_one_winner(tmp_path, shared_store):
    path = tmp_path / "interviews.sqlite3"
    store = store_at(path)
    create(store)
    stores = [store if shared_store else store_at(path) for _ in range(8)]
    barrier = Barrier(8)

    def answer(index):
        barrier.wait(timeout=5)
        try:
            return stores[index].update(KEY, 0, answers=[{"answer": index}], pending=None)
        except ValueError as exc:
            assert "conflict" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(answer, range(8)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert winners[0]["revision"] == 1
    assert store_at(path).get(KEY) == winners[0]


def test_atomic_creation_has_exactly_one_winner(tmp_path):
    path = tmp_path / "interviews.sqlite3"
    stores = [store_at(path) for _ in range(8)]
    barrier = Barrier(8)

    def enter(store):
        barrier.wait(timeout=5)
        try:
            return create(store)
        except ValueError as exc:
            assert "already exists" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(enter, stores))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert store_at(path).get(KEY) == winners[0]


def test_exit_phase_always_invalidates_pending_question(tmp_path):
    store = store_at(tmp_path / "interviews.sqlite3")
    create(store)
    store.update(KEY, 0, pending={"id": "old nonce"})
    tombstone = store.update(KEY, 1, phase="exited")
    assert tombstone["pending"] is None
    assert store.get(KEY) == tombstone


def test_exited_record_with_pending_action_is_corrupt(tmp_path):
    path = tmp_path / "interviews.sqlite3"
    store = store_at(path)
    state = create(store)
    state.update(phase="exited", pending={"id": "stale"})
    corrupt(path, json.dumps(state))
    with pytest.raises(ValueError, match="Corrupt interview"):
        store.get(KEY)
    with pytest.raises(ValueError, match="Corrupt interview"):
        create(store)


def test_reopening_in_fresh_process_preserves_snapshot(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "interviews.sqlite3"
    store = store_at(path)
    create(store)
    state = store.update(KEY, 0, phase="awaiting_plan_decision", summary="Requirements",
                         pending={"id": "plan-decision"}, answers=[{"answer": "A"}])
    script = (
        "import json,sys; from pathlib import Path; "
        "from gateway.interview_store import InterviewStore; "
        "print(json.dumps(InterviewStore(Path(sys.argv[1])).get(sys.argv[2])))"
    )
    result = subprocess.run([sys.executable, "-c", script, str(path), KEY],
                            capture_output=True, text=True, check=True, timeout=10)
    assert json.loads(result.stdout) == state
