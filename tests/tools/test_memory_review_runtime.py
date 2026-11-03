"""Real conversation loop and executors; only model/network boundaries are fake."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from tests.tools.test_memory_review import home
from tests.run_agent.test_run_agent import agent, TestRunConversation as _LoopTests, _mock_response, _mock_tool_call
from tools import memory_review as mr, write_approval as wa
from tools.memory_tool import load_on_disk_store, MEMORY_SCHEMA


def ingress(text, mid):
    return SimpleNamespace(source=SimpleNamespace(user_id="OWNER", scope_id="T1", chat_id="C1", thread_id="10.1", platform="slack"),
                           message_id=mid, text=text, raw_message={"text": text, "ts": mid}, internal=False)


def test_real_main_conversation_selects_earlier_source_not_latest_ingress(home, agent):
    _LoopTests()._setup_agent(agent)
    agent._memory_store = load_on_disk_store()
    agent.valid_tool_names.add("memory")
    agent._memory_enabled = True
    agent._persist_disabled = True
    agent.tools.append({"type": "function", "function": copy.deepcopy(MEMORY_SCHEMA)})
    frozen_prompt = agent._cached_system_prompt
    frozen_tools = copy.deepcopy(agent.tools)
    agent.client.chat.completions.create.return_value = _mock_response(content="Understood")
    mr.bind_ingress(ingress("Please use compact bullets with evidence", "10.1"))
    mr.bind_session(agent.session_id)
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        first = agent.run_conversation("Please use compact bullets with evidence")
        catalog = getattr(agent, "_memory_review_sources", {})
        assert catalog, "Turn context must retain immutable attributable user sources"
        source_id = next(k for k, v in catalog.items() if v["excerpt"] == "Please use compact bullets with evidence")
        call = _mock_tool_call(name="memory", arguments=json.dumps({"target": "user", "action": "add", "content": "Prefers compact bullets with evidence", "source_refs": [source_id]}), call_id="memory1")
        agent.client.chat.completions.create.side_effect = [
            _mock_response(finish_reason="tool_calls", tool_calls=[call]), _mock_response(content="Done")]
        mr.bind_ingress(ingress("Thanks, next topic", "11.1"))
        mr.bind_session(agent.session_id)
        second = agent.run_conversation("Thanks, next topic", conversation_history=first["messages"])
    assert second["completed"]
    rec = wa.list_pending("memory")[0]
    evidence = rec["review"]["triggers"]
    assert [t["excerpt"] for t in evidence] == ["Please use compact bullets with evidence"]
    assert evidence[0]["source_id"] == source_id
    assert evidence[0]["attribution"] == "model_selected_runtime_source"
    assert evidence[0]["message_id"] == "10.1"
    assert "Thanks, next topic" not in mr.review_text(rec)
    assert agent._cached_system_prompt == frozen_prompt
    assert agent.tools == frozen_tools
    assert "source_refs" in MEMORY_SCHEMA["parameters"]["properties"]
    wire = agent.client.chat.completions.create.call_args_list
    first_user = next(m for m in wire[0].kwargs["messages"] if m["role"] == "user")
    replay_user = next(m for m in wire[1].kwargs["messages"] if m["role"] == "user")
    assert first_user == replay_user, "Historical API bytes must survive turn finalization"
    assert not load_on_disk_store().user_entries


@pytest.mark.parametrize("refs", [None, ["invented-id"], ["invented quote: prefer tables"]])
def test_invalid_attribution_is_not_presented_as_trigger(home, agent, refs):
    agent._memory_store = load_on_disk_store()
    mr.bind_ingress(ingress("Thanks, next topic", "11.1"))
    result = agent._invoke_tool("memory", {"target": "memory", "action": "add", "content": "A fact", "source_refs": refs}, "task1")
    assert json.loads(result)["staged"]
    text = mr.review_text(wa.list_pending("memory")[0])
    assert "Review initiation context; relevance unverified" in text
    assert "Trigger evidence" not in text
    assert "invented quote" not in text


@pytest.mark.asyncio
async def test_background_fork_freezes_catalog_at_spawn_and_uses_real_conversation(home, agent, monkeypatch):
    from agent.background_review import spawn_background_review_thread
    import run_agent
    _LoopTests()._setup_agent(agent)
    agent._memory_store = load_on_disk_store()
    agent._memory_enabled = True
    agent._persist_disabled = True
    mr.bind_ingress(ingress("Please use compact bullets with evidence", "10.1"))
    mr.remember_turn(agent, "Please use compact bullets with evidence")
    original_catalog = copy.deepcopy(agent._memory_review_sources)
    source_id = next(iter(original_catalog))
    messages = [{"role": "user", "content": "Please use compact bullets with evidence"},
                {"role": "assistant", "content": "Understood"},
                {"role": "user", "content": "Thanks, next topic"},
                {"role": "assistant", "content": "Next"}]
    mr.bind_ingress(ingress("Thanks, next topic", "11.1"))
    target, _ = spawn_background_review_thread(agent, messages, review_memory=True)
    # The parent can move on while the worker is waiting to run.
    agent._memory_review_sources = {}
    from run_agent import AIAgent
    original_init = AIAgent.__init__
    forks = []
    def offline_init(self, **kw):
        with patch("run_agent.OpenAI"), patch("run_agent.check_toolset_requirements", return_value={}):
            original_init(self, **kw)
        _LoopTests()._setup_agent(self)
        self.valid_tool_names.add("memory")
        self._memory_manager = None
        self.client = MagicMock()
        self._offline_client = self.client
        call = _mock_tool_call(name="memory", arguments=json.dumps({"target": "user", "action": "add", "content": "Prefers compact bullets with evidence", "source_refs": [source_id]}), call_id="bg-memory")
        self.client.chat.completions.create.side_effect = [_mock_response(finish_reason="tool_calls", tool_calls=[call]), _mock_response(content="Staged")]
        forks.append(self)
    monkeypatch.setattr(AIAgent, "__init__", offline_init)
    from tests.tools.test_memory_review import Slack
    slack = Slack()
    mr.attach_delivery(mr.MemoryReview(slack), asyncio.get_running_loop())
    try:
        await asyncio.to_thread(target)
        for _ in range(100):
            if slack.posts:
                break
            await asyncio.sleep(.01)
        assert slack.posts == 1
    finally:
        mr.detach_delivery()
    records = wa.list_pending("memory")
    assert records, "Real background conversation must execute the memory tool"
    rec = records[0]
    assert rec["origin"] == "background_review"
    assert rec["review"]["triggers"][0]["source_id"] == source_id
    assert rec["review"]["triggers"][0]["excerpt"] == "Please use compact bullets with evidence"
    assert forks[0]._cached_system_prompt == agent._cached_system_prompt
    assert forks[0]._memory_review_sources == original_catalog
    wire = forks[0]._offline_client.chat.completions.create.call_args_list[0].kwargs["messages"]
    assert source_id in wire[-1]["content"]
    assert "Please use compact bullets with evidence" in wire[-1]["content"]
    assert rec["delivery"]["channel"] == "D1"
    assert not load_on_disk_store().user_entries


@pytest.mark.asyncio
async def test_verified_source_channel_permalink_uses_exact_workspace_client(home, agent):
    from tests.tools.test_memory_review import Slack
    from plugins.platforms.slack.adapter import SlackAdapter
    from gateway.config import PlatformConfig
    event = ingress("Prefer compact bullets", "10.1")
    mr.bind_ingress(event)
    mr.bind_session(agent.session_id)
    mr.remember_turn(agent, event.text)
    source_id = next(iter(agent._memory_review_sources))
    agent._memory_store = load_on_disk_store()
    result = json.loads(agent._invoke_tool("memory", {"target": "user", "action": "add", "content": "Prefers compact bullets", "source_refs": [source_id]}, "task1"))
    rec = mr.read(result["pending_id"])
    assert rec["review"]["triggers"][0].get("channel_id") == "C1"
    slack = Slack()
    calls = []
    async def permalink(**kw):
        calls.append(kw)
        return {"ok": True, "permalink": "https://verified.slack.com/archives/C1/p10000001"}
    slack.chat_getPermalink = permalink
    adapter = SlackAdapter(PlatformConfig(enabled=True, token="fake"))
    adapter._team_clients = {"T1": slack, "OTHER": Slack()}
    service = adapter._memory_review_service()
    await service.flush()
    assert calls == [{"channel": "C1", "message_ts": "10.1"}]
    current = mr.read(rec["id"])
    assert current["review"]["triggers"][0]["message_url"] == "https://verified.slack.com/archives/C1/p10000001"
    assert mr.bound_record(current)
    assert "https://verified.slack.com/archives/C1/p10000001" in json.dumps(slack.messages)


def test_mutated_snapshot_cannot_validate_source_id(home, agent):
    mr.bind_ingress(ingress("Prefer bullets", "10.1"))
    mr.remember_turn(agent, "Prefer bullets")
    source_id = next(iter(agent._memory_review_sources))
    agent._memory_review_sources[source_id]["excerpt"] = "Model invented quote"
    evidence = mr.attributed_triggers(agent, [source_id])
    assert all(t.get("attribution") != "model_selected_runtime_source" for t in evidence)
    assert "Model invented quote" not in json.dumps(evidence)
