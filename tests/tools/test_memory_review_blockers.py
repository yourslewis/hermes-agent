"""Independent-review regressions: exercise disk, delivery and runtime boundaries."""
import json
from types import SimpleNamespace

import pytest
from tests.tools.test_memory_review import Slack, home, stage
from tools import memory_review as mr
from tools.memory_tool import load_on_disk_store


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["owner", "profile"])
async def test_recovery_never_discloses_record_outside_persisted_binding(home, drift):
    rec = stage(load_on_disk_store(), action="add", content="OLD PRIVATE FACT")
    slack = Slack()
    if drift == "owner":
        (home / "config.yaml").write_text((home / "config.yaml").read_text().replace("OWNER", "NEW"))
        async def info(**kw):
            return {"ok": True, "channel": {"id": "D1", "is_im": True, "user": "NEW"}}
        async def members(**kw):
            return {"ok": True, "members": ["NEW", "BOT"]}
        slack.conversations_info, slack.conversations_members = info, members
    else:
        rec["review"]["profile"] = "copied-profile"
        mr.save(rec)
    actor = "NEW" if drift == "owner" else "OWNER"
    service = mr.MemoryReview(slack)
    for args in (["review", rec["id"]], ["recover", rec["id"]], []):
        result = await mr.recover_command(service, actor, "T1", "D1", args)
        assert "OLD PRIVATE FACT" not in result
        assert rec["id"] not in result
    assert slack.posts == 0


@pytest.mark.asyncio
async def test_changed_record_binding_blocks_existing_card_updates(home):
    rec = stage(load_on_disk_store(), action="add", content="OLD PRIVATE FACT")
    slack = Slack()
    service = mr.MemoryReview(slack)
    await service.flush()
    before = json.dumps(slack.messages)
    rec = mr.read(rec["id"])
    rec["review"]["profile"] = "copied-profile"
    rec.update(status="Deferred", card_dirty=True)
    mr.save(rec)
    with pytest.raises(PermissionError):
        await service.update_card(rec)
    assert json.dumps(slack.messages) == before


@pytest.mark.asyncio
async def test_committed_write_readback_failure_reconciles_after_restart(home, monkeypatch):
    from tools.memory_tool import MemoryStore
    rec = stage(load_on_disk_store(), action="add", content="Durable fact")
    slack = Slack()
    service = mr.MemoryReview(slack)
    await service.flush()
    button = slack.messages["1.1"]["blocks"][-1]["elements"][0]
    original = MemoryStore._read_raw_checked
    def transient(path):
        if path.exists() and path.read_bytes() == b"Durable fact":
            return "", False
        return original(path)
    monkeypatch.setattr(MemoryStore, "_read_raw_checked", staticmethod(transient))
    outcome = await service.callback({"user": {"id": "OWNER"}, "team": {"id": "T1"},
        "channel": {"id": "D1"}, "message": {"ts": "1.1"}, "actions": [button]})
    assert (home / "memories" / "MEMORY.md").read_bytes() == b"Durable fact"
    assert outcome == "applying"
    assert mr.read(rec["id"])["status"] == "applying"
    await mr.MemoryReview(slack).flush()
    assert mr.read(rec["id"])["status"] == "applying"
    monkeypatch.setattr(MemoryStore, "_read_raw_checked", staticmethod(original))
    await mr.MemoryReview(slack).flush()
    assert mr.read(rec["id"])["status"] == "Applied"
    assert "Applied" in json.dumps(slack.messages["1.1"])


@pytest.mark.asyncio
async def test_lf_to_crlf_drift_invalidates_exact_review(home):
    store = load_on_disk_store()
    store.add("memory", "First line\nSecond line")
    rec = stage(store, action="replace", old_text="First", content="Replacement")
    slack = Slack()
    service = mr.MemoryReview(slack)
    await service.flush()
    path = home / "memories" / "MEMORY.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    button = slack.messages["1.1"]["blocks"][-1]["elements"][0]
    result = await service.callback({"user": {"id": "OWNER"}, "team": {"id": "T1"},
        "channel": {"id": "D1"}, "message": {"ts": "1.1"}, "actions": [button]})
    assert result == "Stale"
    assert path.read_bytes() == b"First line\r\nSecond line"


@pytest.mark.asyncio
async def test_old_card_update_failure_does_not_block_new_immediate_proposal(home):
    first = stage(load_on_disk_store(), action="add", content="First fact")
    slack = Slack()
    service = mr.MemoryReview(slack)
    await service.flush()
    first = mr.read(first["id"])
    first.update(status="Deferred", card_dirty=True)
    mr.save(first)
    async def broken_update(**kw):
        raise OSError("temporary Slack update outage")
    slack.chat_update = broken_update
    second = stage(load_on_disk_store(), action="add", content="Second fact")
    await service.flush()
    assert slack.posts == 2
    assert mr.read(second["id"])["delivery"]["ts"] == "2.1"
    old = mr.read(first["id"])
    assert old["card_dirty"] is True
    assert old["delivery_error"]
    slack.chat_update = Slack.chat_update.__get__(slack)
    await mr.MemoryReview(slack).flush()
    assert not mr.read(first["id"])["card_dirty"]
    assert not mr.read(first["id"]).get("delivery_error")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["Stale", "Failed"])
async def test_explicit_recover_delivers_undelivered_terminal_with_original_key(home, terminal):
    rec = stage(load_on_disk_store(), action="add", content="A fact")
    rec.update(sending=True)
    mr.save(rec)
    slack = Slack()
    service = mr.MemoryReview(slack)
    if terminal == "Stale":
        load_on_disk_store().add("memory", "External change")
    else:
        rec.update(status="Failed", error="Current budget exceeded")
        mr.save(rec)
    await service.flush()
    assert slack.posts == 0
    assert mr.read(rec["id"])["status"] == terminal
    await mr.recover_command(service, "OWNER", "T1", "D1", ["recover", rec["id"]])
    assert slack.posts == 1
    assert slack.messages["1.1"]["client_msg_id"] == rec["notification_id"]
    assert all(block["type"] != "actions" for block in slack.messages["1.1"]["blocks"])
