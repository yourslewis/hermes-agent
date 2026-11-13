"""Immediate durable outbox and immutable source capture integration."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from tests.tools.test_memory_review import Slack, home, stage
from tools.memory_tool import load_on_disk_store, memory_tool
from tools import write_approval as wa


@pytest.mark.asyncio
async def test_background_stage_notifies_before_turn_end(home):
    from tools.memory_review import MemoryReview, attach_delivery, detach_delivery
    slack = Slack()
    attach_delivery(MemoryReview(slack), asyncio.get_running_loop())
    try:
        result = await asyncio.to_thread(memory_tool, action="add", content="A background fact", store=load_on_disk_store())
        assert json.loads(result)["staged"]
        # Notification is scheduled directly by persistence, not a turn-end callback.
        for _ in range(100):
            if slack.posts:
                break
            await asyncio.sleep(.01)
        assert slack.posts == 1
    finally:
        detach_delivery()


def test_frozen_trigger_context_survives_parent_mutation(home):
    from tools.memory_review import capture_trigger, proposal_context
    source = SimpleNamespace(user_id="OWNER", thread_id="THREAD", chat_id="C1", platform="slack")
    event = SimpleNamespace(source=source, message_id="42.1", text="Prefer bullets", raw_message={"team": "T1", "ts": "42.1"})
    trigger = capture_trigger(event, "SESSION")
    event.text = "Unrelated next turn"
    source.thread_id = "NEXT"
    with proposal_context([trigger]):
        rec = stage(load_on_disk_store(), action="add", content="Prefers bullets")
    assert rec["review"]["triggers"][0]["excerpt"] == "Prefer bullets"
    assert rec["review"]["triggers"][0]["thread_id"] == "THREAD"
    assert rec["review"]["triggers"][0]["classification"] == "unavailable"


@pytest.mark.asyncio
async def test_adapter_routes_only_configured_workspace(home):
    from plugins.platforms.slack.adapter import SlackAdapter
    from gateway.config import PlatformConfig
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    slack = Slack()
    adapter._team_clients = {"OTHER": slack}
    adapter._app = SimpleNamespace(client=slack)
    assert adapter._memory_review_service() is None
    adapter._team_clients["T1"] = slack
    assert adapter._memory_review_service().client is slack


def test_runtime_provenance_is_copied_before_staging(home):
    provenance = [{"excerpt": "Actual user utterance", "classification": "inferred", "session_id": "s1"}]
    result = json.loads(memory_tool(action="add", content="A fact", store=load_on_disk_store(), provenance=provenance))
    provenance[0]["excerpt"] = "mutated"
    rec = wa.get_pending("memory", result["pending_id"])
    assert rec["review"]["triggers"][0]["excerpt"] == "Actual user utterance"


def test_background_thread_freezes_ingress_evidence(home, monkeypatch):
    from tools.memory_review import proposal_context, agent_triggers
    from agent.background_review import spawn_background_review_thread
    seen = []
    monkeypatch.setattr("agent.background_review._run_review_in_thread", lambda agent, msgs, prompt, **kw: seen.append(agent_triggers(agent)))
    parent = SimpleNamespace(session_id="s1")
    with proposal_context([{"excerpt": "First turn", "classification": "unavailable"}]):
        target, _ = spawn_background_review_thread(parent, [], review_memory=True)
    with proposal_context([{"excerpt": "Next turn"}]):
        target()
    assert seen[0][0]["excerpt"] == "First turn"


def test_staged_background_summary_never_claims_saved_or_exposes_content(home):
    from agent.background_review import summarize_background_review_actions
    result = memory_tool(action="add", content="Private fact", store=load_on_disk_store())
    messages = [{"role": "assistant", "tool_calls": [{"id": "m1", "function": {"name": "memory", "arguments": json.dumps({"action": "add", "content": "Private fact"})}}]},
                {"role": "tool", "tool_call_id": "m1", "content": result}]
    assert summarize_background_review_actions(messages, [], "verbose") == []


@pytest.mark.asyncio
async def test_real_background_fork_stages_and_notifies_private(home, monkeypatch):
    from tools.memory_review import MemoryReview, attach_delivery, detach_delivery, proposal_context
    from agent.background_review import spawn_background_review_thread
    from tests.agent.test_background_review import _bare_agent
    import run_agent
    parent = _bare_agent()
    parent._memory_store = load_on_disk_store()
    parent.memory_notifications = "verbose"
    seen = []
    parent.background_review_callback = seen.append
    class Fork:
        def __init__(self, **kw): self._session_messages = []
        def run_conversation(self, **kw):
            result = memory_tool(action="add", content="A background fact", store=self._memory_store)
            self._session_messages = [{"role": "tool", "tool_call_id": "new", "content": result}]
        def shutdown_memory_provider(self): pass
        def close(self): pass
    monkeypatch.setattr(run_agent, "AIAgent", Fork)
    slack = Slack()
    attach_delivery(MemoryReview(slack), asyncio.get_running_loop())
    try:
        with proposal_context([{"session_id": "s1", "excerpt": "Remember this", "classification": "unavailable"}]):
            target, _ = spawn_background_review_thread(parent, [], review_memory=True)
        await asyncio.to_thread(target)
        for _ in range(100):
            if slack.posts: break
            await asyncio.sleep(.01)
        assert slack.posts == 1
        rec = wa.list_pending("memory")[0]
        assert rec["origin"] == "background_review"
        assert rec["review"]["triggers"][0]["excerpt"] == "Remember this"
        assert not load_on_disk_store().memory_entries
        assert not seen  # never echo private proposal via parent's shared-channel callback
    finally:
        detach_delivery()
