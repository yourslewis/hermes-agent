"""Independent local review: barriers only, no command execution/network."""
import asyncio
import contextvars
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
from tools import approval as a
from gateway.run_turn_runner import TurnRunner


def capture(key, notify):
    with a.gateway_approval_origin(key, notify):
        return contextvars.copy_context()


def decision(key):
    return a._human_decision(
        replace(a._COMMAND_GATE, transport=False), command="REVIEW-NOT-EXECUTED",
        description="review", pattern_key="review", pattern_keys=["review"], warnings=[],
        session_key=key, approval_callback=lambda *_: "deny", is_cli=False,
        is_gateway=True, is_ask=False,
    )


@pytest.fixture(autouse=True)
def bounded(monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 3)
    yield
    a.unregister_gateway_notify(None)


@pytest.mark.parametrize("teardown", ["stop", "clear", "shutdown"])
def test_revocation_after_callback_snapshot_prevents_adapter_send(teardown):
    """Stop after route snapshots notifier, before it invokes the real notifier."""
    key = "review-send-" + teardown
    snapshotted, proceed = threading.Event(), threading.Event()
    deliveries = []

    class SlackStub:
        def pause_typing_for_chat(self, chat_id):
            pass
        async def send_exec_approval(self, **kwargs):
            deliveries.append(kwargs)
            return SimpleNamespace(success=True, message_id="fake")

    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(
        session_key=key, _status_adapter=SlackStub(), _status_chat_id="C_ORIGIN",
        _status_thread_metadata={"thread_id": "1700.100", "user_id": "U_ORIGIN"},
    )
    runner._close_native_stream_boundary = lambda *_: None
    def schedule(coro, *_):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future
    runner._schedule = schedule
    context = capture(key, runner._approval_notify_sync)
    code = a._GatewayOriginRoute.__call__.__code__
    def trace(frame, event, arg):
        # A line event on the call instruction occurs after releasing _lock and
        # copying self.notify. Never patch the production route or its lock.
        if frame.f_code is code and event == "line" and frame.f_lineno == code.co_firstlineno + 5:
            snapshotted.set()
            assert proceed.wait(3), "review barrier not released"
        return trace
    def worker():
        sys.settrace(trace)
        try:
            return context.run(decision, key)
        finally:
            sys.settrace(None)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        try:
            assert snapshotted.wait(3), "route snapshot not reached"
            if teardown == "clear":
                a.clear_session(key)
            elif teardown == "shutdown":
                a.unregister_gateway_notify(None)
            else:
                from gateway.run_agent_cache import GatewayAgentCacheMixin
                fake = SimpleNamespace(_peek_session_state=lambda _: None,
                    _invalidate_session_run_generation=lambda *args, **kwargs: 1)
                GatewayAgentCacheMixin._interrupt_running_turn(fake, key,
                    interrupt_reason="stop", invalidation_reason="stop_command")
        finally:
            proceed.set()
        result = future.result(timeout=5)
    assert result["approved"] is False
    assert not a.has_blocking_approval(key)
    assert deliveries == [], "revoked route sent through original adapter after teardown returned"


@pytest.mark.parametrize("revoked", [False, True])
def test_origin_cannot_coalesce_into_replacement_turn_consent(monkeypatch, revoked):
    """An old route must not inherit another turn's consent without origin notification."""
    from tools import approval_gateway_wait as wait
    key = "review-revoked-coalesce"
    old_sends, new_sends = [], []
    old_published = threading.Event()
    def old_notify(data):
        old_sends.append(data)
        old_published.set()
        a.resolve_gateway_approval(key, "deny", request_id=data["request_id"])
    old_context = capture(key, old_notify)
    if revoked:
        a.unregister_gateway_notify(key)
    published, following, release = threading.Event(), threading.Event(), threading.Event()
    def new_notify(data):
        new_sends.append(data)
        published.set()
        assert release.wait(3)
    a.register_gateway_notify(key, new_notify)
    original = wait._await_coalesced_leader
    def observed(*args, **kwargs):
        following.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(wait, "_await_coalesced_leader", observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(decision, key)
        assert published.wait(3)
        follower = pool.submit(old_context.run, decision, key)
        try:
            if revoked:
                assert follower.result(timeout=2)["approved"] is False
            else:
                assert old_published.wait(2), "live origin did not publish its own request"
                assert follower.result(timeout=2)["approved"] is False
            assert not following.is_set(), "different generations coalesced"
            assert a.resolve_gateway_approval(key, "session", request_id=new_sends[0]["request_id"]) == 1
        finally:
            release.set()
        current_result = leader.result(timeout=5)
        stale_result = follower.result(timeout=5)
    assert current_result["approved"] is True
    assert len(old_sends) == (0 if revoked else 1)
    assert len(new_sends) == 1
    assert stale_result["approved"] is False, "revoked origin inherited replacement turn's session consent"


@pytest.mark.parametrize("teardown", ["stop", "shutdown"])
def test_pending_wait_fails_closed_on_teardown(teardown):
    key = "review-pending-" + teardown
    notified, release = threading.Event(), threading.Event()
    def notify(data):
        notified.set()
        assert release.wait(3)
    context = capture(key, notify)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(context.run, decision, key)
        try:
            assert notified.wait(3)
            a.unregister_gateway_notify(None if teardown == "shutdown" else key)
        finally:
            release.set()
        result = future.result(timeout=5)
    assert result["approved"] is False
    assert result["outcome"] == "cancelled"
    assert not a.has_blocking_approval(key)
