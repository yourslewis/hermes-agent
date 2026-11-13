"""Security and crash recovery using actual memory and proposal files."""
import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.tools.test_memory_review import Slack, home, stage
from tools.memory_tool import load_on_disk_store, memory_tool
from tools import write_approval as wa
from tools.memory_review import MemoryReview, decide, read, save, journal_lock


async def card_for(**kwargs):
    rec = stage(load_on_disk_store(), **kwargs)
    slack = Slack()
    service = MemoryReview(slack)
    await service.flush()
    return rec, slack, service


def body(slack, which=0):
    return {"user": {"id": "OWNER"}, "team": {"id": "T1"}, "channel": {"id": "D1"},
            "message": {"ts": "1.1"}, "actions": [slack.messages["1.1"]["blocks"][-1]["elements"][which]]}


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("user", "OTHER"), ("team", "T2"), ("channel", "C1"), ("message", "2.1")])
async def test_exact_owner_workspace_private_dm_and_card(home, field, value):
    rec, slack, service = await card_for(action="add", content="A fact")
    payload = body(slack)
    payload[field]["ts" if field == "message" else "id"] = value
    with pytest.raises(PermissionError):
        await service.callback(payload)
    assert not load_on_disk_store().memory_entries
    assert read(rec["id"])["status"] == "Pending"


@pytest.mark.asyncio
async def test_profile_and_proposal_revision_binding(home, tmp_path, monkeypatch):
    rec, slack, service = await card_for(action="add", content="A fact")
    payload = body(slack)
    rec = read(rec["id"])
    rec["review"]["after_raw"] = "tampered"
    save(rec)
    with pytest.raises(PermissionError):
        await service.callback(payload)
    monkeypatch.setenv("HERMES_HOME", str(home / "other"))
    with pytest.raises(PermissionError):
        await service.callback(payload)


@pytest.mark.asyncio
async def test_stale_target_never_retargets_selector(home):
    store = load_on_disk_store()
    store.add("memory", "Likes tea with milk")
    rec, slack, service = await card_for(action="replace", old_text="tea", content="Likes coffee")
    store.replace("memory", "tea", "Likes tea with honey")
    assert await service.callback(body(slack)) == "Stale"
    assert load_on_disk_store().memory_entries == ["Likes tea with honey"]


@pytest.mark.asyncio
@pytest.mark.parametrize("choice,outcome", [(1, "Rejected"), (2, "Deferred")])
async def test_reject_defer_persist_without_resend(home, choice, outcome):
    rec, slack, service = await card_for(action="add", content="A fact")
    assert await service.callback(body(slack, choice)) == outcome
    await MemoryReview(slack).flush()
    assert slack.posts == 1
    assert not load_on_disk_store().memory_entries
    assert read(rec["id"])["status"] == outcome


@pytest.mark.asyncio
async def test_commit_crash_reconciles_without_replaying(home):
    rec, slack, service = await card_for(action="add", content="A fact")
    payload = body(slack)
    rec = read(rec["id"])
    rec["status"] = "applying"
    save(rec)
    load_on_disk_store().add("memory", "A fact")
    await MemoryReview(slack).flush()
    assert read(rec["id"])["status"] == "Applied"
    assert load_on_disk_store().memory_entries == ["A fact"]
    assert "Applied" in json.dumps(slack.messages["1.1"])


@pytest.mark.asyncio
async def test_concurrent_decisions_are_one_terminal_transition(home):
    rec, slack, service = await card_for(action="add", content="A fact")
    rec = read(rec["id"])
    def click(action):
        return decide(rec["id"], rec["review"]["binding"], action, "OWNER", "T1", "D1", "1.1")["status"]
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(click, ["approve", "reject"]))
    assert outcomes[0] == outcomes[1]
    actual = load_on_disk_store().memory_entries
    assert actual == (["A fact"] if outcomes[0] == "Applied" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["public", "group", "other_owner", "workspace", "members"])
async def test_unverified_destination_never_receives_memory(home, bad):
    rec = stage(load_on_disk_store(), action="add", content="Private fact")
    slack = Slack()
    if bad == "workspace":
        async def auth(): return {"ok": True, "team_id": "OTHER", "user_id": "BOT"}
        slack.auth_test = auth
    elif bad == "members":
        async def members(**kw): return {"ok": True, "members": ["OWNER", "BOT", "OTHER"]}
        slack.conversations_members = members
    else:
        async def info(**kw):
            return {"ok": True, "channel": {"id": "D1", "is_im": bad != "public", "is_mpim": bad == "group", "user": "OTHER" if bad == "other_owner" else "OWNER"}}
        slack.conversations_info = info
    await MemoryReview(slack).flush()
    assert slack.posts == 0


@pytest.mark.parametrize("kw", [
    {"action": "add", "content": "same"},
    {"action": "add", "content": "x" * 2300},
    {"action": "remove", "old_text": "missing"},
    {"operations": [{"action": "replace", "old_text": "same", "content": "new"}, {"action": "remove", "old_text": "same"}]},
])
def test_preflight_invalid_never_staged(home, kw):
    load_on_disk_store().add("memory", "same")
    result = json.loads(memory_tool(store=load_on_disk_store(), **kw))
    assert result["success"] is False
    assert not wa.list_pending("memory")
    assert load_on_disk_store().memory_entries == ["same"]


@pytest.mark.asyncio
async def test_sequential_batch_previews_complete_entries_and_applies(home):
    store = load_on_disk_store()
    store.add("memory", "Alpha full entry")
    rec, slack, service = await card_for(operations=[
        {"action": "replace", "old_text": "Alpha", "content": "Beta full entry"},
        {"action": "remove", "old_text": "Beta"},
        {"action": "add", "content": "Gamma"},
    ])
    assert rec["review"]["changes"][1] == {"action": "remove", "before": "Beta full entry", "after": None}
    assert await service.callback(body(slack)) == "Applied"
    assert load_on_disk_store().memory_entries == ["Gamma"]


def test_managed_proposal_id_is_path_safe(home):
    outside = home / "outside.json"
    outside.write_text('{"secret": "must not read"}')
    assert wa.get_pending("memory", "../../outside") is None


def test_enabled_review_forces_staging_even_with_inline_callback(home):
    from tools.terminal_tool import set_approval_callback
    set_approval_callback(lambda *args, **kw: "once")
    try:
        result = json.loads(memory_tool(action="add", content="A fact", store=load_on_disk_store()))
        assert result.get("staged")
        assert not load_on_disk_store().memory_entries
    finally:
        set_approval_callback(None)
