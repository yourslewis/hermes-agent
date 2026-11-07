"""Recoverability, outbox safety, and preparation defaults."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from tests.tools.test_memory_review import Slack, home, stage
from tools import write_approval as wa
from tools.memory_review import MemoryReview, read
from tools.memory_tool import load_on_disk_store, memory_tool


def test_prepare_defaults_disabled_without_changing_gate(home):
    from hermes_cli.config import DEFAULT_CONFIG
    assert DEFAULT_CONFIG["memory"]["review"]["enabled"] is False
    assert DEFAULT_CONFIG["memory"]["review"]["owner_id"] == ""
    assert DEFAULT_CONFIG["memory"]["write_approval"] is False


def test_repeated_identical_proposal_reuses_outbox_id(home):
    store = load_on_disk_store()
    first = stage(store, action="add", content="A fact")
    second = stage(store, action="add", content="A fact")
    assert first["id"] == second["id"]
    assert len(wa.list_pending("memory")) == 1


@pytest.mark.asyncio
async def test_conflicting_pending_targets_are_blocked(home):
    store = load_on_disk_store()
    store.add("memory", "Alpha full entry")
    first = stage(store, action="replace", old_text="Alpha", content="Beta")
    second = json.loads(memory_tool(store=store, action="remove", old_text="Alpha"))
    assert second["success"] is False
    assert "conflict" in second["error"].lower()


@pytest.mark.asyncio
async def test_private_recover_shows_full_review_without_reposting(home):
    from tools.memory_review import recover_command
    rec = stage(load_on_disk_store(), action="add", content="Sensitive full entry")
    slack = Slack()
    service = MemoryReview(slack)
    assert await recover_command(service, "OWNER", "T1", "C_SHARED", []) == "Use the configured private memory review DM."
    assert slack.posts == 0
    result = await recover_command(service, "OWNER", "T1", "D1", ["review", rec["id"]])
    assert "Sensitive full entry" in result
    await recover_command(service, "OWNER", "T1", "D1", [])
    assert slack.posts == 1
    assert "Review too large" not in result


def test_failed_stage_never_acknowledges_saved_or_pending(home, monkeypatch):
    def fail(*args): raise OSError("disk full")
    monkeypatch.setattr("tools.memory_review.save", fail)
    result = json.loads(memory_tool(action="add", content="A fact", store=load_on_disk_store()))
    assert result["success"] is False
    assert not result.get("staged")
    assert not load_on_disk_store().memory_entries


@pytest.mark.asyncio
async def test_destination_change_cannot_replay_pending_proposal(home):
    rec = stage(load_on_disk_store(), action="add", content="Sensitive full entry")
    text = (home / "config.yaml").read_text().replace("OWNER", "OTHER")
    (home / "config.yaml").write_text(text)
    slack = Slack()
    async def info(**kw): return {"ok": True, "channel": {"id": "D1", "is_im": True, "user": "OTHER"}}
    async def members(**kw): return {"ok": True, "members": ["OTHER", "BOT"]}
    slack.conversations_info = info
    slack.conversations_members = members
    await MemoryReview(slack).flush()
    assert slack.posts == 0


@pytest.mark.asyncio
async def test_payload_tamper_cannot_apply_different_operations(home):
    rec = stage(load_on_disk_store(), action="add", content="Reviewed fact")
    slack = Slack()
    service = MemoryReview(slack)
    await service.flush()
    rec = read(rec["id"])
    rec["payload"]["content"] = "Unreviewed fact"
    from tools.memory_review import save
    save(rec)
    # Payload is not replayed; only immutable reviewed poststate is eligible.
    button = slack.messages["1.1"]["blocks"][-1]["elements"][0]
    result = await service.callback({"user": {"id": "OWNER"}, "team": {"id": "T1"}, "channel": {"id": "D1"}, "message": {"ts": "1.1"}, "actions": [button]})
    assert result == "Applied"
    assert load_on_disk_store().memory_entries == ["Reviewed fact"]


def test_legacy_record_id_path_traversal_is_refused(home):
    path = home / "pending" / "outside.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"secret": "do not disclose"}')
    assert wa.get_pending("memory", "../outside") is None
    assert wa.discard_pending("memory", "../outside") is False
    assert path.exists()


@pytest.mark.asyncio
async def test_card_edit_invalidates_approval_before_memory_write(home):
    rec = stage(load_on_disk_store(), action="add", content="A fact")
    slack = Slack()
    service = MemoryReview(slack)
    await service.flush()
    button = slack.messages["1.1"]["blocks"][-1]["elements"][0]
    slack.messages["1.1"]["blocks"][0]["text"]["text"] = "Different review content"
    with pytest.raises(PermissionError):
        await service.callback({"user": {"id": "OWNER"}, "team": {"id": "T1"}, "channel": {"id": "D1"}, "message": {"ts": "1.1"}, "actions": [button]})
    assert not load_on_disk_store().memory_entries


def test_consolidation_duplicate_keeps_all_trigger_provenance(home):
    from tools.memory_review import proposal_context
    with proposal_context([{"excerpt": "First", "session_id": "s1"}]):
        rec = stage(load_on_disk_store(), action="add", content="A fact")
    with proposal_context([{"excerpt": "Second", "session_id": "s2"}]):
        second = stage(load_on_disk_store(), action="add", content="A fact")
    assert second["id"] == rec["id"]
    assert [t["excerpt"] for t in second["review"]["triggers"]] == ["First", "Second"]


@pytest.mark.asyncio
async def test_uncertain_delivery_requires_explicit_private_recovery(home):
    from tools.memory_review import recover_command, save
    rec = stage(load_on_disk_store(), action="add", content="A fact")
    rec["sending"] = True  # process died after claim, before a known receipt
    save(rec)
    slack = Slack()
    service = MemoryReview(slack)
    await service.flush()
    assert slack.posts == 0
    result = await recover_command(service, "OWNER", "T1", "D1", ["recover", rec["id"]])
    assert slack.posts == 1
    assert read(rec["id"])["delivery"]["ts"] == "1.1"


@pytest.mark.asyncio
async def test_copied_other_profile_record_is_never_delivered(home):
    from tools.memory_review import save, digest
    rec = stage(load_on_disk_store(), action="add", content="Other profile private fact")
    rec["review"]["profile"] = "other-profile"
    rec["review"]["binding"] = digest({k: v for k, v in rec["review"].items() if k != "binding"})
    save(rec)
    slack = Slack()
    await MemoryReview(slack).flush()
    assert slack.posts == 0


def test_oversized_review_has_complete_local_review_not_truncated(home):
    from hermes_cli.write_approval_commands import handle_pending_subcommand
    from tools.memory_review import proposal_context
    with proposal_context([{"excerpt": "E" * 150000}]):
        rec = stage(load_on_disk_store(), action="add", content="A fact")
    out = handle_pending_subcommand("memory", ["review", rec["id"]])
    assert "A fact" in out
    assert "E" * 150000 in out


@pytest.mark.asyncio
async def test_gateway_memory_private_recovery_uses_scope_id(home):
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from gateway.platforms.base import MessageEvent
    from gateway.config import Platform
    rec = stage(load_on_disk_store(), action="add", content="A fact")
    slack = Slack()
    service = MemoryReview(slack)
    handler = GatewaySlashCommandsMixin.__new__(GatewaySlashCommandsMixin)
    handler._session_key_for_source = lambda source: "session"
    handler._adapter_for_source = lambda source: SimpleNamespace(_memory_review_service=lambda: service)
    source = SimpleNamespace(user_id="OWNER", chat_id="D1", platform=Platform.SLACK, scope_id="T1")
    event = MessageEvent(text="/memory", source=source, raw_message={})
    out = await handler._handle_memory_command(event)
    assert rec["id"] in out
    assert slack.posts == 1
    source.chat_id = "C_SHARED"
    assert await handler._handle_memory_command(event) == "Use the configured private memory review DM."


def test_prepare_helper_is_read_only_and_profile_generic(home):
    from tools.memory_review import preparation_report
    before = (home / "config.yaml").read_bytes()
    report = preparation_report()
    assert report["profile_home"] == str(home)
    assert report["configured"] is True
    assert report["network_verified"] is False
    assert (home / "config.yaml").read_bytes() == before
