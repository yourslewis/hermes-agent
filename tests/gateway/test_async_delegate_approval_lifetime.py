"""Origin-thread routing after the foreground turn exits; no real transports or commands."""
import contextvars
from concurrent.futures import Future
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent.delegation_context import delegated_child_context
from gateway.run_turn_runner import TurnRunner
from tools import approval as a


@pytest.fixture(autouse=True)
def bounded_approval_wait(monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 0)


def _decision(key):
    return a._human_decision(
        replace(a._COMMAND_GATE, transport=False), command="DIAGNOSTIC-NOT-EXECUTED",
        description="diagnostic", pattern_key="diagnostic", pattern_keys=["diagnostic"], warnings=[],
        session_key=key, approval_callback=lambda *_: "deny", is_cli=False, is_gateway=True, is_ask=False,
    )


def _capture(key, notify):
    with a.gateway_approval_origin(key, notify):
        return contextvars.copy_context()


def test_origin_timeout_does_not_grant_or_leave_pending(monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 0)
    notify = Mock()
    result = _capture("origin-timeout", notify).run(_decision, "origin-timeout")
    assert result["approved"] is False
    assert result["outcome"] == "timeout"
    assert notify.call_count == 1
    assert not a.has_blocking_approval("origin-timeout")


def test_children_keep_origin_when_newer_foreground_overlaps():
    key = "origin-overlap"
    old = Mock(side_effect=lambda data: a.resolve_gateway_approval(key, "deny", request_id=data["request_id"]))
    newer = Mock()
    context = _capture(key, old)
    a.register_gateway_notify(key, newer)
    try:
        a.unregister_gateway_notify(key, preserve_origin=True, expected_cb=old)
        assert a._gateway_notify_cbs[key] is newer
        for _ in range(2):
            assert context.copy().run(_decision, key)["outcome"] == "denied"
        assert old.call_count == 2
        newer.assert_not_called()
    finally:
        a.unregister_gateway_notify(key)


def test_revoked_origin_never_borrows_replacement_callback():
    key = "origin-revoked"
    old, newer = Mock(), Mock()
    context = _capture(key, old)
    a.unregister_gateway_notify(key)
    a.register_gateway_notify(key, newer)
    try:
        assert context.run(_decision, key)["outcome"] == "notify_failed"
        old.assert_not_called()
        newer.assert_not_called()
        assert not a.has_blocking_approval(key)
    finally:
        a.unregister_gateway_notify(key)


def test_context_cannot_borrow_other_session():
    context = _capture("origin-A", Mock())
    with pytest.raises(RuntimeError, match="does not match"):
        context.run(_decision, "origin-B")


def test_gateway_shutdown_revokes_all_copied_routes():
    old = Mock()
    context = _capture("origin-shutdown", old)
    a.unregister_gateway_notify(None)
    assert context.run(_decision, "origin-shutdown")["outcome"] == "notify_failed"
    old.assert_not_called()


def test_stop_core_revokes_idle_delegate_origin():
    from gateway.run_agent_cache import GatewayAgentCacheMixin
    key = "origin-stop"
    old = Mock()
    context = _capture(key, old)
    runner = SimpleNamespace(
        _peek_session_state=lambda _: None,
        _invalidate_session_run_generation=lambda *args, **kwargs: 1,
    )
    GatewayAgentCacheMixin._interrupt_running_turn(
        runner, key, interrupt_reason="stop", invalidation_reason="stop_command",
    )
    assert context.run(_decision, key)["outcome"] == "notify_failed"
    old.assert_not_called()

@pytest.mark.parametrize("choice", ["deny", "once"])
def test_retained_notifier_delivers_to_original_slack_thread_and_author(choice):
    import asyncio
    key = "origin-delivery-" + choice
    deliveries = []

    class SlackStub:
        def pause_typing_for_chat(self, chat_id):
            pass

        async def send_exec_approval(self, **kwargs):
            deliveries.append(kwargs)
            with a._lock:
                request_id = a._gateway_queues[key][0].data["request_id"]
            assert a.resolve_gateway_approval(key, choice, request_id=request_id)
            return SimpleNamespace(success=True, message_id="card")

    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(
        session_key=key, _status_adapter=SlackStub(), _status_chat_id="C_ORIGIN",
        _status_thread_metadata={"thread_id": "1700.100", "user_id": "U_ALICE", "scope_id": "T_ORIGIN"},
    )
    runner._close_native_stream_boundary = lambda *_: None
    def schedule(coro, *_):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future
    runner._schedule = schedule
    context = _capture(key, runner._approval_notify_sync)
    newer = Mock()
    a.register_gateway_notify(key, newer)
    try:
        result = context.run(_decision, key)
        assert len(deliveries) == 1
        assert deliveries[0]["chat_id"] == "C_ORIGIN"
        assert deliveries[0]["metadata"] == {
            "thread_id": "1700.100", "user_id": "U_ALICE", "scope_id": "T_ORIGIN",
        }
        assert result["approved"] is (choice == "once")
        newer.assert_not_called()
    finally:
        a.unregister_gateway_notify(key)


def test_async_delegate_notifies_origin_once_after_foreground_exit():
    key = "async-approval-lifetime-regression"
    sent = []
    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(
        session_key=key, session_id="origin-session", source=SimpleNamespace(user_id="U_ALICE", user_name="Alice"),
        persist_user_display_kind=None, persist_user_display_metadata=None, moa_config=None,
        inbound_message_id=None, mute_notification_reply=False,
    )
    runner._native_image_run_message = lambda: "delegate"
    def notify(data):
        sent.append(data)
        a.resolve_gateway_approval(key, "deny", request_id=data["request_id"])
    runner._approval_notify_sync = notify
    captured = []
    agent = SimpleNamespace(run_conversation=lambda *args, **kwargs: captured.append(contextvars.copy_context()))
    with patch("gateway.run._wrap_current_message_with_observed_context", side_effect=lambda message, _: message):
        runner._run_conversation_with_approval(agent, [], None, None, None)
    assert key not in a._gateway_notify_cbs
    def child():
        with delegated_child_context("child-session"):
            return a._human_decision(
                replace(a._COMMAND_GATE, transport=False), command="DIAGNOSTIC-NOT-EXECUTED",
                description="diagnostic", pattern_key="diagnostic", pattern_keys=["diagnostic"], warnings=[],
                session_key=key, approval_callback=lambda *_: "deny", is_cli=False, is_gateway=True,
                is_ask=False,
            )
    try:
        result = captured[0].run(child)
        assert len(sent) == 1
        assert result["approved"] is False
        assert result["outcome"] == "denied"
        assert not a.has_blocking_approval(key)
        assert key not in a._pending
    finally:
        with a._lock:
            a._pending.pop(key, None)
            a._gateway_queues.pop(key, None)
