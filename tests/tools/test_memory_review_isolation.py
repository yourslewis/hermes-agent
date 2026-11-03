"""Isolation regressions across owner reconfiguration and copied profile homes."""
import json
import shutil

import pytest
from tests.tools.test_memory_review import Slack, home, stage
from tools import memory_review as mr
from tools.memory_tool import load_on_disk_store
from tests.tools.test_memory_review_runtime import ingress
from tests.run_agent.test_run_agent import agent, TestRunConversation as _LoopTests, _mock_response, _mock_tool_call
from types import SimpleNamespace
from unittest.mock import patch, MagicMock
import copy


@pytest.mark.parametrize("drift", ["owner", "profile", "profile_owner"])
@pytest.mark.parametrize("executor", ["invoke", "main_loop", "background"])
def test_real_executors_reject_foreign_catalog_and_stale_ingress(home, agent, monkeypatch, drift, executor):
    _LoopTests()._setup_agent(agent)
    agent._memory_store = load_on_disk_store()
    agent.valid_tool_names.add("memory")
    agent._memory_enabled = True
    agent._persist_disabled = True
    mr.bind_ingress(ingress("OLD PRIVATE SNAPSHOT", "10.1"))
    mr.remember_turn(agent, "OLD PRIVATE SNAPSHOT")
    source_id = next(iter(agent._memory_review_sources))
    old_catalog = copy.deepcopy(agent._memory_review_sources)
    frozen_prompt, frozen_tools = agent._cached_system_prompt, copy.deepcopy(agent.tools)
    args = {"target": "user", "action": "add", "content": "A current fact", "source_refs": [source_id]}
    call = _mock_tool_call(name="memory", arguments=json.dumps(args), call_id="isolation")
    if executor == "background":
        from agent.background_review import spawn_background_review_thread
        target, _ = spawn_background_review_thread(agent, [
            {"role": "user", "content": "Already cached historical turn"},
            {"role": "assistant", "content": "Understood"}], review_memory=True)
    active = change_scope(home, monkeypatch, drift)
    # Delayed workers keep the old ingress context: neither it nor its catalog
    # may become provenance for a new owner just because the config changed.
    if executor == "invoke":
        result = json.loads(agent._invoke_tool("memory", args, "task1"))
        assert result["staged"]
    elif executor == "main_loop":
        mr.bind_ingress(ingress("Current unrelated turn", "11.1"))
        agent.client.chat.completions.create.side_effect = [
            _mock_response(finish_reason="tool_calls", tool_calls=[call]), _mock_response(content="Done")]
        with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
            assert agent.run_conversation("Current unrelated turn")["completed"]
        wire = agent.client.chat.completions.create.call_args_list[0].kwargs["messages"]
        assert "OLD PRIVATE SNAPSHOT" not in json.dumps(wire)
    else:
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
            self.client.chat.completions.create.side_effect = [
                _mock_response(finish_reason="tool_calls", tool_calls=[call]), _mock_response(content="Done")]
            forks.append(self)
        monkeypatch.setattr(AIAgent, "__init__", offline_init)
        target()
        assert forks
        wire = forks[0]._offline_client.chat.completions.create.call_args_list[0].kwargs["messages"]
        assert "OLD PRIVATE SNAPSHOT" not in json.dumps(wire)
        assert forks[0]._cached_system_prompt == frozen_prompt
        assert forks[0]._memory_review_sources == old_catalog
    from tools import write_approval as wa
    records = wa.list_pending("memory")
    assert len(records) == 1
    assert "OLD PRIVATE SNAPSHOT" not in mr.review_text(records[0])
    assert records[0]["review"]["triggers"][0].get("attribution") != "model_selected_runtime_source"
    assert agent._cached_system_prompt == frozen_prompt
    assert agent.tools == frozen_tools


@pytest.mark.parametrize("drift", ["owner", "profile", "team", "channel"])
@pytest.mark.parametrize("review_enabled", [True, False])
def test_source_consumers_reject_old_scope_even_when_review_disabled(home, monkeypatch, drift, review_enabled):
    agent = SimpleNamespace(session_id="same-session")
    mr.bind_ingress(ingress("OLD PRIVATE SNAPSHOT", "10.1"))
    mr.remember_turn(agent, "OLD PRIVATE SNAPSHOT")
    source_id = next(iter(agent._memory_review_sources))
    active = change_scope(home, monkeypatch, drift)
    if not review_enabled:
        config = active / "config.yaml"
        config.write_text(config.read_text().replace("enabled: true", "enabled: false"))
    mr.bind_ingress(ingress("Current unrelated turn", "11.1"))
    assert "OLD PRIVATE SNAPSHOT" not in mr.source_instructions(agent)
    evidence = mr.attributed_triggers(agent, [source_id])
    assert "OLD PRIVATE SNAPSHOT" not in json.dumps(evidence)
    assert all(t.get("attribution") != "model_selected_runtime_source" for t in evidence)


def test_direct_staging_does_not_relabel_stale_ingress(home, monkeypatch):
    mr.bind_ingress(ingress("OLD PRIVATE SNAPSHOT", "10.1"))
    change_scope(home, monkeypatch, "owner")
    rec = stage(load_on_disk_store(), action="add", content="A fact")
    assert "OLD PRIVATE SNAPSHOT" not in mr.review_text(rec)


@pytest.mark.parametrize("drift", ["owner", "profile", "team", "channel"])
def test_restart_catalog_is_scoped_and_preserves_current_sources(home, monkeypatch, drift):
    agent = SimpleNamespace(session_id="same-session")
    mr.bind_ingress(ingress("OLD PRIVATE SNAPSHOT", "10.1"))
    mr.remember_turn(agent, "OLD PRIVATE SNAPSHOT")
    old_id = next(iter(agent._memory_review_sources))
    old_path = next((home / "pending" / "memory_sources").glob("*.json"))
    old_bytes = old_path.read_bytes()
    active = change_scope(home, monkeypatch, drift)
    fresh = SimpleNamespace(session_id="same-session")
    mr.bind_ingress(ingress("CURRENT SOURCE", "11.1"))
    mr.remember_turn(fresh, "CURRENT SOURCE")
    assert old_id not in fresh._memory_review_sources
    assert "OLD PRIVATE SNAPSHOT" not in mr.source_instructions(fresh)
    current_id = next(iter(fresh._memory_review_sources))
    assert mr.attributed_triggers(fresh, [current_id])[0]["attribution"] == "model_selected_runtime_source"
    restarted = SimpleNamespace(session_id="same-session")
    mr.bind_ingress(ingress("Another current turn", "12.1"))
    mr.remember_turn(restarted, "Another current turn")
    assert current_id in restarted._memory_review_sources
    assert old_path.read_bytes() == old_bytes
    assert (active / "pending" / "memory_sources" / old_path.name).read_bytes() == old_bytes


@pytest.mark.parametrize("catalog_kind", ["legacy", "foreign_snapshot", "foreign_catalog", "corrupt_hash"])
def test_catalog_and_each_snapshot_fail_closed_without_relabeling(home, catalog_kind):
    agent = SimpleNamespace(session_id="same-session")
    mr.bind_ingress(ingress("PRIVATE SNAPSHOT", "10.1"))
    mr.remember_turn(agent, "PRIVATE SNAPSHOT")
    source_id = next(iter(agent._memory_review_sources))
    snapshot = copy.deepcopy(agent._memory_review_sources[source_id])
    path = next((home / "pending" / "memory_sources").glob("*.json"))
    envelope = json.loads(path.read_text())
    if catalog_kind == "legacy":
        snapshot.pop("scope", None)
    elif catalog_kind == "foreign_snapshot":
        snapshot["scope"]["destination"]["owner_id"] = "OTHER"
    elif catalog_kind == "foreign_catalog":
        envelope["scope"]["destination"]["owner_id"] = "OTHER"
    else:
        snapshot["excerpt"] = "CORRUPTED PRIVATE SNAPSHOT"
    if catalog_kind != "corrupt_hash":
        source_id = mr.digest({k: v for k, v in snapshot.items() if k != "source_id"})
        snapshot["source_id"] = source_id
    envelope["sources"] = {source_id: snapshot}
    path.write_text(json.dumps(envelope if catalog_kind != "legacy" else {source_id: snapshot}))
    # Exercise both direct injection and persisted reload validation.
    if catalog_kind != "foreign_catalog":
        agent._memory_review_sources = {source_id: snapshot}
        assert "PRIVATE SNAPSHOT" not in mr.source_instructions(agent)
        mr.bind_ingress(ingress("Current turn", "11.1"))
        assert "PRIVATE SNAPSHOT" not in json.dumps(mr.attributed_triggers(agent, [source_id]))
    restarted = SimpleNamespace(session_id="same-session")
    mr.bind_ingress(ingress("Current turn", "11.1"))
    mr.remember_turn(restarted, "Current turn")
    assert source_id not in restarted._memory_review_sources
    assert "PRIVATE SNAPSHOT" not in mr.source_instructions(restarted)


def change_scope(home, monkeypatch, drift):
    if drift in {"profile", "profile_owner"}:
        copied = home.parent / (home.name + "-copied")
        shutil.copytree(home, copied)
        monkeypatch.setenv("HERMES_HOME", str(copied))
        if drift == "profile_owner":
            config = copied / "config.yaml"
            config.write_text(config.read_text().replace("OWNER", "NEW"))
        return copied
    config = home / "config.yaml"
    old, new = {"owner": ("OWNER", "NEW"), "team": ("T1", "T2"),
                "channel": ("D1", "D2")}[drift]
    config.write_text(config.read_text().replace(old, new))
    return home


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["owner", "profile", "profile_owner", "team", "channel"])
async def test_real_turn_rejects_ingress_changed_before_capture_to_card(home, agent, monkeypatch, drift):
    """No earlier catalog or fresh bind: reproduce the in-flight ingress race."""
    from tools import write_approval as wa
    _LoopTests()._setup_agent(agent)
    agent._memory_store = load_on_disk_store()
    agent._memory_enabled = True
    agent._persist_disabled = True
    secret = "OLD OWNER PRIVATE INGRESS"
    mr.bind_ingress(ingress(secret, "10.1"))
    active = change_scope(home, monkeypatch, drift)
    selected = []

    def model(**kw):
        # Select exactly the source that the actual turn constructor exposes,
        # then exercise the actual direct executor, not a helper-only assertion.
        selected.extend(getattr(agent, "_memory_review_sources", {}))
        result = json.loads(agent._invoke_tool("memory", {
            "target": "user", "action": "add", "content": "A current fact",
            "source_refs": selected or ["unavailable"]}, "task1"))
        assert result["staged"]
        return _mock_response(content="Done")

    agent.client.chat.completions.create.side_effect = model
    with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
        assert agent.run_conversation(secret)["completed"]
    record = wa.list_pending("memory")[0]
    slack = Slack()
    cfg = mr.settings()

    async def auth(**kw):
        return {"ok": True, "team_id": cfg["team_id"], "user_id": "BOT"}

    async def info(**kw):
        return {"ok": True, "channel": {"id": cfg["channel_id"], "is_im": True, "user": cfg["owner_id"]}}

    async def members(**kw):
        return {"ok": True, "members": [cfg["owner_id"], "BOT"]}

    async def post(**kw):
        slack.posts += 1
        slack.messages["1.1"] = dict(kw, ts="1.1")
        return {"ok": True, "channel": cfg["channel_id"], "ts": "1.1"}

    slack.auth_test, slack.conversations_info = auth, info
    slack.conversations_members, slack.chat_postMessage = members, post
    await mr.MemoryReview(slack).flush()
    persisted = mr.read(record["id"])
    assert persisted is not None
    assert slack.posts == 1
    assert persisted["delivery"]["channel"] == cfg["channel_id"]
    # The raw user turn remains unchanged for prompt-cache correctness. Only
    # newly manufactured source instructions/snapshots/provenance are forbidden.
    wire = agent.client.chat.completions.create.call_args.kwargs["messages"]
    user = next(m["content"] for m in wire if m["role"] == "user")
    assert user.count(secret) == 1
    assert secret not in mr.source_instructions(agent)
    assert secret not in json.dumps(agent._memory_review_sources)
    for path in (active / "pending" / "memory_sources").glob("*.json"):
        assert secret not in path.read_text()
    assert secret not in json.dumps(mr.attributed_triggers(agent, selected))
    assert secret not in mr.review_text(persisted)
    assert secret not in json.dumps(slack.messages)
    assert persisted["review"]["triggers"][0]["classification"] == "unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["owner", "profile", "profile_owner"])
@pytest.mark.parametrize("proposal", ["duplicate", "conflict"])
async def test_staging_ignores_foreign_pending_content(home, monkeypatch, drift, proposal):
    store = load_on_disk_store()
    store.add("memory", "Original fact")
    with mr.proposal_context([{"excerpt": "OLD OWNER PRIVATE SOURCE"}]):
        old = stage(store, action="replace", old_text="Original", content="Replacement")
    old_path = home / "pending" / "memory" / (old["id"] + ".json")
    old_bytes = old_path.read_bytes()
    active = change_scope(home, monkeypatch, drift)
    with mr.proposal_context([{"excerpt": "NEW OWNER PRIVATE SOURCE"}]):
        new = stage(load_on_disk_store(), action="replace", old_text="Original",
                    content="Replacement" if proposal == "duplicate" else "Different replacement")
    assert new["id"] != old["id"]
    assert (active / "pending" / "memory" / old_path.name).read_bytes() == old_bytes
    assert old_path.read_bytes() == old_bytes
    slack = Slack()
    if drift in {"owner", "profile_owner"}:
        async def info(**kw):
            return {"ok": True, "channel": {"id": "D1", "is_im": True, "user": "NEW"}}
        async def members(**kw):
            return {"ok": True, "members": ["NEW", "BOT"]}
        slack.conversations_info, slack.conversations_members = info, members
    await mr.MemoryReview(slack).flush()
    assert slack.posts == 1
    assert mr.read(new["id"])["delivery"]["ts"] == "1.1"
    assert "OLD OWNER PRIVATE SOURCE" not in json.dumps(slack.messages)
    assert "NEW OWNER PRIVATE SOURCE" in json.dumps(slack.messages)
    if drift == "owner":
        config = active / "config.yaml"
        config.write_text(config.read_text().replace("NEW", "OWNER"))
        assert "NEW OWNER PRIVATE SOURCE" not in mr.review_text(mr.read(old["id"]))


def test_explicit_empty_ingress_does_not_restore_agent_fallback(home):
    agent = SimpleNamespace(session_id="s1", _memory_review_triggers=[{
        "scope": mr.source_scope(), "excerpt": "REJECTED AGENT FALLBACK"}])
    with mr.proposal_context([]):
        mr.remember_turn(agent, "RAW TEXT MUST NOT BECOME EVIDENCE")
        assert not agent._memory_review_sources
        assert "REJECTED AGENT FALLBACK" not in json.dumps(mr.attributed_triggers(agent, None))


def test_real_background_fork_preserves_explicit_empty_source_capture(home, agent, monkeypatch):
    from agent.background_review import spawn_background_review_thread
    from run_agent import AIAgent
    _LoopTests()._setup_agent(agent)
    agent._memory_store = load_on_disk_store()
    agent._memory_enabled = True
    agent._persist_disabled = True
    agent._memory_review_sources = {}
    with mr.proposal_context([]):
        target, _ = spawn_background_review_thread(agent, [], review_memory=True)
    mr.bind_ingress(ingress("LATER PRIVATE PARENT SOURCE", "11.1"))
    mr.remember_turn(agent, "LATER PRIVATE PARENT SOURCE")
    assert agent._memory_review_sources
    original_init = AIAgent.__init__
    forks = []

    def offline_init(self, **kw):
        with patch("run_agent.OpenAI"), patch("run_agent.check_toolset_requirements", return_value={}):
            original_init(self, **kw)
        _LoopTests()._setup_agent(self)
        self._memory_manager = None
        self.client = MagicMock()
        self._offline_client = self.client
        self.client.chat.completions.create.return_value = _mock_response(content="Done")
        forks.append(self)

    monkeypatch.setattr(AIAgent, "__init__", offline_init)
    target()
    assert forks
    assert forks[0]._memory_review_sources == {}
    wire = forks[0]._offline_client.chat.completions.create.call_args.kwargs["messages"]
    assert "LATER PRIVATE PARENT SOURCE" not in json.dumps(wire)


@pytest.mark.parametrize("capture", [None, [], [{"excerpt": "UNBOUND PRIVATE INGRESS"}]])
def test_unverifiable_ingress_and_real_cli_remain_unavailable(home, agent, capture):
    """Absent CLI, deliberately empty, and legacy unbound are not raw sources."""
    from tools import write_approval as wa
    _LoopTests()._setup_agent(agent)
    agent._memory_store = load_on_disk_store()
    agent._memory_enabled = True
    agent._persist_disabled = True
    token = mr._trigger_context.set(capture)
    try:
        agent.client.chat.completions.create.return_value = _mock_response(content="Done")
        with patch.object(agent, "_save_trajectory"), patch.object(agent, "_cleanup_task_resources"):
            assert agent.run_conversation("UNVERIFIABLE RAW TEXT")["completed"]
        result = json.loads(agent._invoke_tool("memory", {
            "target": "memory", "action": "add", "content": "A CLI fact"}, "task1"))
        assert result["staged"]
        record = wa.get_pending("memory", result["pending_id"])
        assert record is not None
        assert record["review"]["triggers"][0]["classification"] == "unavailable"
        assert not agent._memory_review_sources
        assert not mr.source_instructions(agent)
        text = mr.review_text(record)
        assert "UNBOUND PRIVATE INGRESS" not in text
        assert "UNVERIFIABLE RAW TEXT" not in text
        assert not load_on_disk_store().memory_entries
    finally:
        mr._trigger_context.reset(token)
