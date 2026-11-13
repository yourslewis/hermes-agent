"""Consent commit races; real gate/teardown, synthetic targets, no execution."""
import contextvars
import inspect
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from tools import approval as a


def decide(key, surface="gateway"):
    return a._human_decision(
        replace(a._COMMAND_GATE, transport=surface == "transport"),
        command="TEST-NOT-EXECUTED", description="test", pattern_key="grant-test",
        pattern_keys=["grant-test"], warnings=[("grant-test", "test", False)],
        session_key=key, approval_callback=None, is_cli=surface == "cli",
        is_gateway=surface == "gateway", is_ask=False)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(a, "_permanent_approved", set())
    monkeypatch.setattr(a, "_session_approved", {})
    monkeypatch.setattr(a, "_permanent_baseline_by_home", {})
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 3)
    yield
    a.unregister_gateway_notify(None)


@pytest.mark.parametrize("choice", ["once", "session", "always"])
@pytest.mark.parametrize("surface", ["gateway", "transport", "cli"])
@pytest.mark.parametrize("origin", [True, False])
def test_teardown_after_decision_before_grant(monkeypatch, choice, surface, origin):
    key = f"grant-{choice}-{surface}-{origin}"
    ready, release = threading.Event(), threading.Event()
    saved = []
    monkeypatch.setattr(a, "save_permanent_allowlist", lambda values: saved.append(set(values)))
    monkeypatch.setattr(a, "_present_with_selected_transport", lambda **kw: object())
    monkeypatch.setattr(a, "_transport_choice", lambda *args, **kw: (choice, None))
    monkeypatch.setattr(a, "prompt_dangerous_approval", lambda *args, **kw: choice)
    def notify(data):
        assert a.resolve_gateway_approval(key, choice, request_id=data["request_id"]) == 1
    if origin:
        with a.gateway_approval_origin(key, notify):
            context = contextvars.copy_context()
    else:
        a.register_gateway_notify(key, notify)
        context = contextvars.copy_context()
    lines, start = inspect.getsourcelines(a._human_decision)
    targets = {start + i for i, line in enumerate(lines) if "return grant(choice)" in line}
    def trace(frame, event, arg):
        if event == "line" and frame.f_code is a._human_decision.__code__ and frame.f_lineno in targets:
            ready.set()
            assert release.wait(3)
        return trace
    def worker():
        sys.settrace(trace)
        try:
            return context.run(decide, key, surface)
        finally:
            sys.settrace(None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert ready.wait(3)
            a.clear_session(key)
        finally:
            release.set()
        result = future.result(timeout=3)
    assert result["approved"] is False
    assert result["outcome"] == "cancelled"
    assert not a._session_approved.get(key)
    assert not a._permanent_approved
    assert saved == []


@pytest.mark.parametrize("choice", ["once", "session", "always"])
def test_valid_consent_commits_once_and_keeps_intended_scope(monkeypatch, choice):
    key = "valid-" + choice
    saved = []
    monkeypatch.setattr(a, "save_permanent_allowlist", lambda values: saved.append(set(values)))
    def notify(data):
        assert a.resolve_gateway_approval(key, choice, request_id=data["request_id"]) == 1
    with a.gateway_approval_origin(key, notify):
        assert decide(key)["approved"] is True
    assert a._session_approved.get(key, set()) == ({"grant-test"} if choice != "once" else set())
    assert saved == ([{"grant-test"}] if choice == "always" else [])
    a.clear_session(key)
    assert a.is_approved("replacement", "grant-test") is (choice == "always")


@pytest.mark.parametrize("teardown", ["clear", "stop", "shutdown"])
def test_actual_persistence_mutation_serializes_teardown_without_queue_lock(monkeypatch, teardown):
    """Pause at the real writer's mutation, after all liveness checks."""
    from hermes_cli import config
    key = "persist-" + teardown
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    saved = []
    monkeypatch.setattr(config, "load_config", lambda: {"command_allowlist": []})
    def save(values):
        entered.set()
        assert release.wait(3)
        saved.append(values["command_allowlist"])
    monkeypatch.setattr(config, "save_config", save)
    def notify(data):
        assert a.resolve_gateway_approval(key, "always", request_id=data["request_id"]) == 1
    with a.gateway_approval_origin(key, notify):
        context = contextvars.copy_context()
    def revoke():
        attempted.set()
        if teardown == "clear":
            a.clear_session(key)
        else:
            a.unregister_gateway_notify(None if teardown == "shutdown" else key)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(context.run, decide, key)
        try:
            assert entered.wait(3)
            # Disk I/O must never monopolize the queue/dispatch lock.
            assert a._lock.acquire(timeout=.1)
            a._lock.release()
            cleared = pool.submit(revoke)
            assert attempted.wait(1)
            with pytest.raises(TimeoutError):
                cleared.result(timeout=.05)
        finally:
            release.set()
        assert result.result(timeout=3)["approved"]
        cleared.result(timeout=3)
    assert saved == [["grant-test"]]
    # Already committed permanent consent intentionally survives teardown.
    assert a.is_approved("new-session", "grant-test")
    if teardown == "clear":
        assert not a._session_approved.get(key)


def test_persistence_timeout_does_not_claim_teardown_completed(monkeypatch):
    key = "bounded-persistence"
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr(a, "_CONSENT_COMMIT_TIMEOUT", .03)
    def save(values):
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(a, "save_permanent_allowlist", save)
    def notify(data):
        a.resolve_gateway_approval(key, "always", request_id=data["request_id"])
    with a.gateway_approval_origin(key, notify):
        context = contextvars.copy_context()
    route = context.get(a._gateway_origin_route)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(context.run, decide, key)
        try:
            assert entered.wait(3)
            with pytest.raises(TimeoutError, match="consent commit"):
                a.clear_session(key)
            assert route.notify is not None
        finally:
            release.set()
        assert future.result(timeout=3)["approved"]
    a.clear_session(key)
    assert route.notify is None
    assert not a._session_approved.get(key)


@pytest.mark.parametrize("choice", ["once", "session", "always"])
def test_revoked_origin_cannot_grant_through_selected_transport(monkeypatch, choice):
    key = "already-ended-" + choice
    monkeypatch.setattr(a, "_present_with_selected_transport", lambda **kw: object())
    monkeypatch.setattr(a, "_transport_choice", lambda *args, **kw: (choice, None))
    saved = []
    monkeypatch.setattr(a, "save_permanent_allowlist", lambda values: saved.append(values))
    with a.gateway_approval_origin(key, lambda _: None):
        context = contextvars.copy_context()
    a.approve_session(key, "grant-test")
    a.unregister_gateway_notify(key)
    assert context.run(a.is_approved, key, "grant-test") is False
    assert not context.run(decide, key, "transport")["approved"]
    assert saved == []
