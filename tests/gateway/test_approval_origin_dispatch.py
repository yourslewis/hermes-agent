"""Deterministic lifecycle controls; adapters are in-memory and execute no commands."""
import asyncio
import contextvars
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from tools import approval as a
from gateway.run_turn_runner import TurnRunner
from tests.gateway.test_approval_origin_races import capture, decision


@pytest.mark.parametrize("teardown", ["stop", "clear", "shutdown"])
def test_queued_async_dispatch_checks_origin_at_adapter_boundary(teardown, monkeypatch):
    from tools import approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 0)
    key = "queued-origin-" + teardown
    queued, resume = threading.Event(), threading.Event()
    deliveries = []
    class Adapter:
        def pause_typing_for_chat(self, chat_id):
            pass
        async def send_exec_approval(self, **kwargs):
            deliveries.append(kwargs)
            return SimpleNamespace(success=True)
        async def send(self, *args, **kwargs):
            deliveries.append(kwargs)
            return SimpleNamespace(success=True)
    runner = object.__new__(TurnRunner)
    runner._ctx = SimpleNamespace(session_key=key, _status_adapter=Adapter(),
        _status_chat_id="C_ORIGIN", _status_thread_metadata={"user_id": "U_ORIGIN"})
    runner._close_native_stream_boundary = lambda *_: None
    def schedule(coro, *_):
        queued.set()
        assert resume.wait(3)
        future = Future()
        try:
            future.set_result(asyncio.run(coro))
        except BaseException as exc:
            future.set_exception(exc)
        return future
    runner._schedule = schedule
    context = capture(key, runner._approval_notify_sync)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(context.run, decision, key)
        try:
            assert queued.wait(3)
            if teardown == "clear":
                a.clear_session(key)
            else:
                a.unregister_gateway_notify(None if teardown == "shutdown" else key)
        finally:
            resume.set()
        assert result.result(timeout=5)["approved"] is False
    assert deliveries == []
    assert not a.has_blocking_approval(key)


def test_started_dispatch_is_cancelled_and_drained():
    async def scenario():
        key = "origin-drain"
        started, cancelled = asyncio.Event(), asyncio.Event()
        with a.gateway_approval_origin(key, lambda _: None):
            route = a._gateway_origin_route.get()
        async def send():
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        task = asyncio.create_task(route.dispatch(send))
        await started.wait()
        a.unregister_gateway_notify(None)
        await a.drain_gateway_origin_dispatches()
        assert task.cancelled()
        assert cancelled.is_set()
        assert not a._gateway_origin_dispatches
    asyncio.run(scenario())


@pytest.mark.parametrize("revoke", [False, True])
def test_same_origin_coalescing_preserved_and_revocation_wins_adoption(monkeypatch, revoke):
    from tools import approval_gateway_wait as wait, approval_context
    monkeypatch.setattr(approval_context, "_get_approval_timeout", lambda: 3)
    key = "same-origin-coalescing"
    published, following, release = threading.Event(), threading.Event(), threading.Event()
    sends = []
    def notify(data):
        sends.append(data)
        published.set()
        assert release.wait(3)
    context = capture(key, notify)
    original = wait._await_coalesced_leader
    def observed(*args, **kwargs):
        following.set()
        return original(*args, **kwargs)
    monkeypatch.setattr(wait, "_await_coalesced_leader", observed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(context.copy().run, decision, key)
        assert published.wait(3)
        follower = pool.submit(context.copy().run, decision, key)
        try:
            assert following.wait(3)
            if revoke:
                a.unregister_gateway_notify(key)
                assert a.resolve_gateway_approval(key, "session") == 0
            else:
                assert a.resolve_gateway_approval(key, "session") == 1
        finally:
            release.set()
        assert leader.result(timeout=5)["approved"] is (not revoke)
        assert follower.result(timeout=5)["approved"] is (not revoke)
    assert len(sends) == 1
    assert not a.has_blocking_approval(key)
    a.unregister_gateway_notify(key)
