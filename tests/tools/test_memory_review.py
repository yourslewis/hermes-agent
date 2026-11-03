"""Real disk-to-card-to-decision paths. Slack is the only fake boundary."""
import json
from types import SimpleNamespace

import pytest

from tools.memory_tool import MemoryStore, memory_tool, load_on_disk_store
from tools import write_approval as wa


class Slack:
    def __init__(self):
        self.messages = {}
        self.posts = 0

    async def auth_test(self):
        return {"ok": True, "team_id": "T1", "user_id": "BOT"}

    async def conversations_info(self, **kw):
        return {"ok": True, "channel": {"id": "D1", "is_im": True, "user": "OWNER"}}

    async def conversations_members(self, **kw):
        return {"ok": True, "members": ["OWNER", "BOT"], "response_metadata": {"next_cursor": ""}}

    async def chat_postMessage(self, **kw):
        self.posts += 1
        ts = str(self.posts) + ".1"
        self.messages[ts] = dict(kw, ts=ts)
        return {"ok": True, "ts": ts, "channel": "D1"}

    async def chat_update(self, **kw):
        self.messages[kw["ts"]].update(kw)
        return {"ok": True, "ts": kw["ts"]}

    async def conversations_history(self, **kw):
        return {"ok": True, "messages": [self.messages[kw["latest"]]]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "memory:\n  write_approval: true\n  review:\n    enabled: true\n"
        "    owner_id: OWNER\n    team_id: T1\n    channel_id: D1\n"
    )
    return tmp_path


def stage(store, **kw):
    result = json.loads(memory_tool(store=store, **kw))
    assert result.get("staged"), result
    return wa.get_pending("memory", result["pending_id"])


@pytest.mark.asyncio
async def test_real_replace_stage_private_card_approve_readback(home):
    from tools.memory_review import MemoryReview
    store = load_on_disk_store()
    store.add("user", "Likes concise replies, with evidence.")
    frozen = store.format_for_system_prompt("user")
    rec = stage(store, action="replace", target="user", old_text="concise",
                content="Likes compact bullets, with evidence.")
    assert rec["review"]["changes"][0]["before"] == "Likes concise replies, with evidence."
    assert load_on_disk_store().user_entries == ["Likes concise replies, with evidence."]
    slack = Slack()
    service = MemoryReview(slack)
    await service.flush()
    assert slack.posts == 1
    card = slack.messages["1.1"]
    assert "Likes concise replies, with evidence." in json.dumps(card)
    assert "unavailable" in json.dumps(card)
    button = card["blocks"][-1]["elements"][0]
    result = await service.callback({"user": {"id": "OWNER"}, "team": {"id": "T1"},
        "channel": {"id": "D1"}, "message": {"ts": "1.1"}, "actions": [button]})
    assert result == "Applied"
    assert load_on_disk_store().user_entries == ["Likes compact bullets, with evidence."]
    assert "Applied" in json.dumps(slack.messages["1.1"])
    assert store.format_for_system_prompt("user") == frozen
    # A new service instance simulates gateway restart; no repeated application or card.
    service = MemoryReview(slack)
    assert await service.callback({"user": {"id": "OWNER"}, "team": {"id": "T1"},
        "channel": {"id": "D1"}, "message": {"ts": "1.1"}, "actions": [button]}) == "Applied"
    await service.flush()
    assert slack.posts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["add", "replace"])
@pytest.mark.parametrize("batch", [True, False], ids=["batch", "single"])
@pytest.mark.parametrize("fields", [
    {"new_text": " Beta "},
    {"content": "Gamma", "new_text": "Beta"},
    {"content": None, "new_text": "Beta"},
    {"content": "", "new_text": "Beta"},
    {"content": "   ", "new_text": "Beta"},
], ids=["alias", "content-wins", "null-content", "empty-content", "whitespace-content"])
async def test_new_text_preview_matches_store_and_approved_readback(home, action, batch, fields):
    from tools.memory_review import MemoryReview

    store = load_on_disk_store()
    assert store.add("memory", "Alpha")["success"]
    path = store._path_for("memory")
    before = path.read_bytes()
    op = dict(action=action, old_text="Alpha", **fields)
    if batch:
        direct = store.apply_batch("memory", [op])
    else:
        # Single-op aliases fall back only on None; batches fall back on falsy content.
        content = fields.get("content")
        if content is None:
            content = fields["new_text"]
        direct = (store.add("memory", content) if action == "add"
                  else store.replace("memory", "Alpha", content))
    expected = path.read_bytes()
    path.write_bytes(before)
    store = load_on_disk_store()
    frozen = store.format_for_system_prompt("memory")
    result = json.loads(memory_tool(store=store, operations=[op]) if batch
                        else memory_tool(store=store, action=action, old_text="Alpha", **fields))
    if not direct["success"]:
        assert result["success"] is False
        assert not wa.list_pending("memory")
        assert path.read_bytes() == before
        return
    assert result.get("staged"), result
    rec = wa.get_pending("memory", result["pending_id"])
    assert rec is not None
    assert rec["review"]["after_raw"].encode("utf-8") == expected
    assert path.read_bytes() == before
    slack = Slack()
    service = MemoryReview(slack)
    await service.flush()
    button = slack.messages["1.1"]["blocks"][-1]["elements"][0]
    assert await service.callback({"user": {"id": "OWNER"}, "team": {"id": "T1"},
        "channel": {"id": "D1"}, "message": {"ts": "1.1"}, "actions": [button]}) == "Applied"
    assert path.read_bytes() == expected
    assert load_on_disk_store().memory_entries == rec["review"]["after_entries"]
    applied = wa.get_pending("memory", rec["id"])
    assert applied is not None and applied["status"] == "Applied"
    assert store.format_for_system_prompt("memory") == frozen


@pytest.mark.parametrize("action", ["add", "replace"])
@pytest.mark.parametrize("content", ["ignore previous instructions", "Beta\n§\nGamma"])
@pytest.mark.parametrize("fields", ["alias", "content-wins"])
def test_batch_alias_cannot_bypass_review_content_safeguards(home, action, content, fields):
    store = load_on_disk_store()
    assert store.add("memory", "Alpha")["success"]
    path = store._path_for("memory")
    before = path.read_bytes()
    op = dict(action=action, old_text="Alpha", new_text=content)
    if fields == "content-wins":
        op.update(content=content, new_text="Safe fallback")
    result = json.loads(memory_tool(store=store, operations=[op]))
    assert result["success"] is False
    assert not wa.list_pending("memory")
    assert path.read_bytes() == before


def test_invalid_preflight_returns_error_not_staged(home):
    store = load_on_disk_store()
    store.add("memory", "Alpha one")
    store.add("memory", "Alpha two")
    result = json.loads(memory_tool(action="replace", old_text="Alpha", content="New", store=store))
    assert result["success"] is False
    assert "ambiguous" in result["error"]
    assert not wa.list_pending("memory")


def test_legacy_text_cannot_bypass_review_binding(home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    rec = stage(load_on_disk_store(), action="add", content="A fact")
    out = handle_pending_subcommand("memory", ["approve", rec["id"]], memory_store=load_on_disk_store())
    assert "private review card" in out
    assert not load_on_disk_store().memory_entries
    handle_pending_subcommand("memory", ["reject", rec["id"]])
    assert wa.get_pending("memory", rec["id"])["status"] == "Pending"
