"""Profile-scoped memory review with append-only runtime source attribution.

The proposal JSON is the durable outbox and decision journal. Both decisions
and outbox claims use the same OS lock, and memory CAS uses the memory writer's
lock. A persisted 'applying' state permits recovery after process death.
"""
from __future__ import annotations

import asyncio
import copy
import difflib
import hashlib
import json
import logging
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from hermes_constants import get_hermes_home
from utils import atomic_write_text

_trigger_context: ContextVar[Any] = ContextVar("memory_review_trigger", default=None)
_source_context: ContextVar[Any] = ContextVar("memory_review_sources", default=None)
_delivery = {}
_card_locks = {}


@contextmanager
def proposal_context(triggers):
    captured = copy.deepcopy(triggers)
    if captured:
        for trigger in captured:
            trigger.setdefault("scope", source_scope())
    token = _trigger_context.set(captured)
    try:
        yield
    finally:
        _trigger_context.reset(token)


def capture_trigger(event, session_id=None):
    source = event.source
    raw = event.raw_message if isinstance(event.raw_message, dict) else {}
    metadata = getattr(event, "metadata", None) or {}
    internal = bool(getattr(event, "internal", False))
    text = raw.get("text", event.text) if not internal else None
    return {"scope": source_scope(), "session_id": session_id, "author_id": getattr(source, "user_id", None),
            "workspace_id": getattr(source, "scope_id", None) or metadata.get("slack_team_id") or raw.get("team"),
            "channel_id": getattr(source, "chat_id", None),
            "platform": str(getattr(source, "platform", "")).lower().removeprefix("platform."),
            "thread_id": getattr(source, "thread_id", None),
            "message_id": event.message_id, "timestamp": raw.get("ts"),
            "excerpt": text[:1000] if isinstance(text, str) else None, "classification": "unavailable"}


def source_scope():
    """Exact provenance namespace, independent of the review enable switch."""
    cfg = settings()
    return {"profile": profile_key(),
            "destination": {key: cfg.get(key) for key in ("owner_id", "team_id", "channel_id")}}


def valid_sources(agent, catalog):
    """Fail closed before exposing excerpts, including legacy unbound snapshots."""
    scope = source_scope()
    session = str(getattr(agent, "session_id", ""))
    if not isinstance(catalog, dict):
        return {}
    return {ref: snapshot for ref, snapshot in catalog.items()
            if isinstance(snapshot, dict) and snapshot.get("scope") == scope
            and snapshot.get("session_id") == session
            and snapshot.get("source_id") == ref
            and digest({k: v for k, v in snapshot.items() if k != "source_id"}) == ref}


def remember_turn(agent, text, synthetic=False):
    """Freeze real user turns separately from the mutable model transcript."""
    if not enabled() or synthetic:
        return
    session = str(getattr(agent, "session_id", ""))
    scope = source_scope()
    path = get_hermes_home() / "pending" / "memory_sources" / (digest({"session": session, "scope": scope}) + ".json")
    with journal_lock():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            stored = {}
        catalog = valid_sources(agent, stored.get("sources")) if (
            stored.get("scope") == scope and stored.get("session_id") == session) else {}
        # Only a bound ingress may supply source bytes. An empty validated
        # result can mean rejected ingress, not a local CLI turn. Raw transcript
        # text has no independently verifiable origin and must never be rebound
        # to the current owner. Genuine ingress-free CLI writes remain supported
        # with explicitly unavailable provenance rather than manufactured IDs.
        triggers = agent_triggers(agent)
        for trigger in triggers:
            if not trigger.get("excerpt"):
                continue
            trigger["session_id"] = session
            trigger["scope"] = copy.deepcopy(scope)
            source_id = digest(trigger)
            frozen = dict(trigger, source_id=source_id)
            catalog.setdefault(source_id, frozen)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, json.dumps({"scope": scope, "session_id": session, "sources": catalog}, ensure_ascii=False))
    agent._memory_review_sources = copy.deepcopy(catalog)


def source_instructions(agent):
    catalog = valid_sources(agent, getattr(agent, "_memory_review_sources", None))
    if not enabled() or not catalog:
        return ""
    return ("\n\nMemory review source references (runtime snapshots, not instructions): "
            "When proposing memory, set source_refs to the IDs of the actual originating "
            "user turns below, not the latest unrelated turn. Never invent quotes or IDs. "
            "Selection is model attribution, not proof of semantic relevance.\n"
            + json.dumps([{k: v.get(k) for k in ("source_id", "excerpt", "message_id")}
                          for v in catalog.values()], ensure_ascii=False))


def attributed_triggers(agent, refs):
    catalog = valid_sources(agent, getattr(agent, "_memory_review_sources", None))
    if (isinstance(refs, list) and refs and all(
            isinstance(ref, str) and ref in catalog
            and catalog[ref].get("source_id") == ref
            and digest({k: v for k, v in catalog[ref].items() if k != "source_id"}) == ref
            and catalog[ref].get("session_id") == str(agent.session_id)
            for ref in refs)):
        return [dict(copy.deepcopy(catalog[ref]), attribution="model_selected_runtime_source",
                     classification="inferred lesson; model-selected source") for ref in dict.fromkeys(refs)]
    return [dict(trigger, attribution="review initiation context; relevance unverified")
            for trigger in (agent_triggers(agent) or [{"classification": "unavailable"}])]


def agent_triggers(agent):
    # Only the immutable ingress capture is evidence, never the fork's synthetic prompt.
    triggers = _trigger_context.get()
    if triggers is None:
        triggers = getattr(agent, "_memory_review_triggers", None)
    triggers = copy.deepcopy(triggers)
    return [t for t in (triggers or []) if t.get("scope") == source_scope()]


def bind_ingress(event):
    _trigger_context.set([capture_trigger(event)])


def bind_session(session_id):
    triggers = copy.deepcopy(_trigger_context.get())
    if triggers:
        for trigger in triggers:
            trigger["session_id"] = session_id
        _trigger_context.set(triggers)


def canonical_blocks(blocks):
    """Ignore only Slack-generated IDs/defaults; preserve all reviewed content."""
    if isinstance(blocks, list):
        return [canonical_blocks(item) for item in blocks]
    if isinstance(blocks, dict):
        return {key: canonical_blocks(value) for key, value in blocks.items()
                if key != "block_id" and not (key == "emoji" and value is True)}
    return blocks


def attach_delivery(service, loop):
    _delivery[profile_key()] = (service, loop)


def detach_delivery():
    _delivery.pop(profile_key(), None)


def notify_staged():
    item = _delivery.get(profile_key())
    if item and not item[1].is_closed():
        future = asyncio.run_coroutine_threadsafe(item[0].flush(), item[1])
        def done(result):
            try:
                result.result()
            except Exception:
                logging.getLogger(__name__).exception("Private memory review delivery failed; recover with /memory")
        future.add_done_callback(done)

def settings():
    from hermes_cli.config import load_config_readonly
    return (load_config_readonly().get("memory") or {}).get("review") or {}


def enabled():
    return settings().get("enabled") is True


def preparation_report():
    """Offline, read-only fleet preparation for the active HERMES_HOME only."""
    from tools import write_approval as wa
    cfg = {}
    path = get_hermes_home() / "config.yaml"
    if path.exists():
        # Do not call load_config: even its readonly view bootstraps a home.
        from hermes_cli.config import read_user_config_raw
        cfg = (read_user_config_raw(path).get("memory") or {})
    review = cfg.get("review") or {}
    records = wa.list_pending("memory")
    return {"profile_home": str(get_hermes_home()), "enabled": review.get("enabled") is True,
            "gate_enabled": cfg.get("write_approval") is True,
            "configured": all(isinstance(review.get(k), str) and bool(review[k]) for k in ("owner_id", "team_id", "channel_id")),
            "network_verified": False,
            "pending": len(records),
            "legacy_pending": sum("review" not in r for r in records),
            "note": "No writes, messages, or verification requests performed. Verify a named private canary before fleet activation."}


def profile_key():
    return hashlib.sha256(str(get_hermes_home().resolve()).encode()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextmanager
def journal_lock():
    from tools.memory_tool import MemoryStore
    with MemoryStore._file_lock(get_hermes_home() / "pending" / "memory" / "journal"):
        yield


def save(record):
    path = get_hermes_home() / "pending" / "memory" / (record["id"] + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2))


def read(pid):
    if not isinstance(pid, str) or not re.fullmatch(r"[a-f0-9]{8,32}", pid):
        return None
    path = get_hermes_home() / "pending" / "memory" / (pid + ".json")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_exact(store, path):
    """Called under the memory lock; UTF-8 decode without newline translation."""
    _, ok = store._read_raw_checked(path)
    if not ok:
        return "", False
    try:
        return path.read_bytes().decode("utf-8"), True
    except FileNotFoundError:
        return "", True
    except (OSError, UnicodeError):
        return "", False


def prepare(payload, provenance=None):
    """Resolve every substring ONCE to a complete entry and freeze disk bytes."""
    from tools.memory_tool import load_on_disk_store, ENTRY_DELIMITER, _scan_memory_content
    store = load_on_disk_store()
    target = payload.get("target", "memory")
    if target not in {"memory", "user"}:
        raise ValueError("Invalid target")
    path = store._path_for(target)
    with store._file_lock(path):
        raw, ok = read_exact(store, path)
        if not ok:
            raise ValueError("Memory file unreadable")
        before = store._parse_entries(raw)
        if raw.strip() != ENTRY_DELIMITER.join(before):
            raise ValueError("Memory file has external drift")
        working = list(before)
        changes = []
        ops = payload.get("operations") if payload.get("action") == "batch" else [payload]
        if not isinstance(ops, list) or not ops:
            raise ValueError("Empty operations")
        for op in ops:
            if not isinstance(op, dict):
                raise ValueError("Malformed operation")
            act = op.get("action")
            content = (op.get("content") or "").strip()
            old = (op.get("old_text") or "").strip()
            if act not in {"add", "replace", "remove"}:
                raise ValueError("Unknown operation")
            if act != "remove":
                if not content or ENTRY_DELIMITER in content:
                    raise ValueError("Empty content or embedded entry delimiter")
                threat = _scan_memory_content(content)
                if threat:
                    raise ValueError(threat)
            prior = None
            if act != "add":
                matches = [i for i, entry in enumerate(working) if old and old in entry]
                if len(matches) != 1:
                    raise ValueError("Stale or ambiguous target: expected exactly one entry")
                idx = matches[0]
                prior = working[idx]
                if act == "remove":
                    working.pop(idx)
                else:
                    working[idx] = content
            else:
                working.append(content)
            if len(set(working)) != len(working) or prior == content:
                raise ValueError("Duplicate or unchanged entry")
            changes.append({"action": act, "before": prior,
                            "after": None if act == "remove" else content})
        after_raw = ENTRY_DELIMITER.join(working)
        if len(after_raw) > store._char_limit(target):
            raise ValueError("Result exceeds character budget")
    # Never label a recent user utterance as an explicit correction without
    # evidence of that relationship. The runtime can supply known classification.
    review = {"target": target, "before_raw": raw, "after_raw": after_raw,
              "before_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
              "after_sha256": hashlib.sha256(after_raw.encode("utf-8")).hexdigest(),
              "encoding": "utf-8",
              "before_entries": before, "after_entries": working, "changes": changes,
              "limit": store._char_limit(target), "profile": profile_key(),
              "profile_label": get_hermes_home().name,
              "destination": {key: settings().get(key) for key in ("owner_id", "team_id", "channel_id")},
              "triggers": [t for t in (provenance or []) if t.get("scope") == source_scope()] or [{"classification": "unavailable"}],
              "rationale": "unavailable", "recommendation": "Review before applying",
              "lost_information": [c["before"] for c in changes if c["before"]],
              "warnings": ["Semantic conflicts and information loss require owner review; no model assessment available."]}
    review["binding"] = digest(review)
    return review


def bound_record(record):
    """Authorize persisted scope before any disclosure or recovery."""
    review = (record or {}).get("review") or {}
    return bool(review and review.get("profile") == profile_key()
                and review.get("destination") == {key: settings().get(key) for key in ("owner_id", "team_id", "channel_id")}
                and review.get("binding") == digest({k: v for k, v in review.items() if k != "binding"}))


def review_text(record):
    """Complete review text, including cards too large for Slack."""
    review = record["review"]
    lines = [f"Memory review · {review['profile_label']} · {record['id']}",
             f"{review['target'].upper()}.md · {record.get('status', 'Pending')}"]
    for i, change in enumerate(review["changes"], 1):
        lines.extend([f"{i}. {change['action'].title()}",
                      "A — current entry:\n" + (change["before"] or "(no prior entry)"),
                      "B — resulting entry:\n" + (change["after"] or "Entry deleted")])
        if change["before"] and change["after"]:
            lines.append("Changes:\n" + "\n".join(difflib.unified_diff(
                change["before"].splitlines(), change["after"].splitlines(), lineterm="")))
    verified = all(t.get("attribution") == "model_selected_runtime_source" for t in review["triggers"])
    lines.append("Trigger evidence (model-attributed runtime snapshots; relevance not independently verified):"
                 if verified else "Review initiation context; relevance unverified (not trigger evidence):")
    for trigger in review["triggers"]:
        for field in ("source_id", "attribution", "session_id", "author_id", "workspace_id", "channel_id", "thread_id", "message_id", "timestamp", "excerpt", "classification", "message_url"):
            lines.append(f"{field}: {trigger.get(field) or 'unavailable'}")
    lines.extend(["Model rationale: " + review["rationale"],
                  "Recommendation: " + review["recommendation"],
                  "Removed/replaced information (full A above): " +
                  ("Review all prior entry text for lost facts." if review["lost_information"] else "none"),
                  f"Budget: {len(review['after_raw'])}/{review['limit']} chars",
                  *review["warnings"]])
    if record.get("error"):
        lines.append("Outcome: " + record["error"])
    return "\n".join(lines)


def render(record):
    """Plain-text sections avoid markup/mention injection and never truncate A/B."""
    review = record["review"]
    text = review_text(record)
    # Slack plain_text section maximum is 3000 chars, 50 blocks/message.
    chunks = [text[i:i + 2900] for i in range(0, len(text), 2900)]
    if len(chunks) > 48:
        raise ValueError("Review too large for a complete Slack card; use local /memory review")
    blocks = [{"type": "section", "text": {"type": "plain_text", "text": chunk}} for chunk in chunks]
    if record.get("status", "Pending") in {"Pending", "Deferred"}:
        value = record["id"] + ":" + review["binding"]
        blocks.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": action},
             "action_id": "hermes_memory_" + action.lower(), "value": value}
            for action in ("Approve", "Reject", "Defer")]})
    return blocks


def decide(pid, binding, action, actor, team, channel, ts):
    """Authenticated exact-proposal decision, serialized with all other decisions."""
    from tools.memory_tool import load_on_disk_store
    cfg = settings()
    if not enabled() or (actor, team, channel) != (cfg.get("owner_id"), cfg.get("team_id"), cfg.get("channel_id")):
        raise PermissionError("Not the configured private review owner/destination")
    with journal_lock():
        rec = read(pid)
        if not rec or rec.get("review", {}).get("profile") != profile_key():
            raise PermissionError("Unknown proposal/profile")
        review = rec["review"]
        signed = {k: v for k, v in review.items() if k != "binding"}
        if binding != review["binding"] or digest(signed) != binding:
            raise PermissionError("Proposal changed; review again")
        if review["destination"] != {key: cfg.get(key) for key in ("owner_id", "team_id", "channel_id")}:
            raise PermissionError("Review destination changed; stage a new proposal")
        delivery = rec.get("delivery") or {}
        if (delivery.get("ts"), delivery.get("channel"), delivery.get("team"), delivery.get("owner")) != (ts, channel, team, actor):
            raise PermissionError("Decision is not bound to this review card")
        if action not in {"approve", "reject", "defer"}:
            raise ValueError("Unknown decision")
        if rec.get("status") in {"Applied", "Rejected", "Stale", "Failed"}:
            return rec
        if rec.get("status") == "applying":
            action = "approve"  # reconcile the durable winning decision, never overwrite it
        if action == "defer":
            rec["status"] = "Deferred"
        elif action == "reject":
            rec["status"] = "Rejected"
        else:
            store = load_on_disk_store()
            path = store._path_for(review["target"])
            with store._file_lock(path):
                raw, ok = read_exact(store, path)
                if not ok:
                    if rec.get("status") == "applying":
                        rec["error"] = "Apply outcome unverified; reconciliation pending: memory unreadable"
                    else:
                        rec.update(status="Failed", error="Memory unreadable; nothing applied")
                elif rec.get("status") == "applying" and raw == review["after_raw"]:
                    rec["status"] = "Applied"
                    rec.pop("error", None)
                elif raw != review["before_raw"]:
                    rec.update(status="Stale", error="Target changed; stage a new proposal and review again")
                elif len(review["after_raw"]) > store._char_limit(review["target"]):
                    rec.update(status="Failed", error="Current character budget exceeded")
                else:
                    rec.update(status="applying", decided_by=actor, decided_at=time.time())
                    save(rec)  # write-ahead record BEFORE the memory write
                    try:
                        atomic_write_text(path, review["after_raw"])
                        actual, ok = read_exact(store, path)
                        if not ok or actual != review["after_raw"]:
                            raise RuntimeError("Readback does not match reviewed result")
                        rec["status"] = "Applied"
                    except Exception as exc:
                        # A replace may have committed even if write/readback raises.
                        # Keep the write-ahead winner retryable until disk is readable.
                        rec.update(status="applying", error="Apply outcome unverified; reconciliation pending: " + str(exc))
        rec.update(decided_by=actor, decided_at=time.time(), card_dirty=True)
        save(rec)
        return rec


class MemoryReview:
    """Slack boundary; only called with a Bolt-authenticated workspace client."""
    def __init__(self, client):
        self.client = client

    async def verify_private(self):
        cfg = settings()
        if not enabled() or not all(cfg.get(k) for k in ("owner_id", "team_id", "channel_id")):
            return False
        auth = await self.client.auth_test()
        if not auth.get("ok") or auth.get("team_id") != cfg["team_id"]:
            return False
        info = await self.client.conversations_info(channel=cfg["channel_id"])
        dm = info.get("channel") or {}
        if not info.get("ok") or not dm.get("is_im") or dm.get("is_mpim") or dm.get("user") != cfg["owner_id"] or dm.get("id") != cfg["channel_id"]:
            return False
        members = await self.client.conversations_members(channel=cfg["channel_id"])
        return bool(members.get("ok") and not (members.get("response_metadata") or {}).get("next_cursor")
                    and set(members.get("members") or []) == {cfg["owner_id"], auth.get("user_id")})

    async def verify_card(self, rec):
        if not bound_record(rec):
            raise PermissionError("Review scope changed")
        delivery = rec["delivery"]
        response = await self.client.conversations_history(channel=delivery["channel"],
            latest=delivery["ts"], inclusive=True, limit=1)
        messages = response.get("messages") or []
        return bool(response.get("ok") and messages and messages[0].get("ts") == delivery["ts"]
                    and canonical_blocks(messages[0].get("blocks")) == canonical_blocks(render(rec)))

    async def update_card(self, rec):
        key = (asyncio.get_running_loop(), profile_key(), rec["id"])
        lock = _card_locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = read(rec["id"])
            if not current:
                return
            await self._update_card(current)

    async def _update_card(self, rec):
        if not bound_record(rec) or not await self.verify_private():
            raise PermissionError("Review scope changed")
        delivery = rec["delivery"]
        response = await self.client.chat_update(channel=delivery["channel"], ts=delivery["ts"],
            text="Memory review: " + rec.get("status", "Pending"), blocks=render(rec))
        if not response.get("ok") or not await self.verify_card(rec):
            raise RuntimeError("Review card update not verified")
        with journal_lock():
            current = read(rec["id"])
            if current and current.get("status") == rec.get("status"):
                current["card_dirty"] = False
                current.pop("delivery_error", None)
                current.pop("delivery_retry_at", None)
                save(current)

    async def flush(self):
        from tools import write_approval as wa
        if not await self.verify_private():
            return
        cfg = settings()
        for old in wa.list_pending("memory", include_decided=True):
            try:
                await self._flush_record(old, cfg)
            except Exception as exc:
                # A failed old update must not starve newly staged proposals.
                with journal_lock():
                    current = read(old["id"])
                    if current:
                        current["delivery_error"] = type(exc).__name__
                        current["delivery_retry_at"] = time.time()
                        save(current)
                logging.getLogger(__name__).warning("Memory review delivery deferred for %s", old["id"])

    async def source_links(self, record):
        """Use only the authenticated configured workspace, never guess a host."""
        updates = {}
        for trigger in record["review"]["triggers"]:
            if (trigger.get("message_url") or trigger.get("platform") != "slack"
                    or trigger.get("workspace_id") != settings().get("team_id")
                    or not trigger.get("channel_id") or not trigger.get("message_id")):
                continue
            try:
                response = await self.client.chat_getPermalink(
                    channel=trigger["channel_id"], message_ts=trigger["message_id"])
                from urllib.parse import urlparse
                url = response.get("permalink")
                parsed = urlparse(url) if isinstance(url, str) else None
                if (response.get("ok") and parsed and parsed.scheme == "https"
                        and parsed.hostname and parsed.hostname.endswith(".slack.com")
                        and not parsed.username and not parsed.password):
                    updates[digest(trigger)] = url
            except Exception:
                pass  # link unavailable must not prevent private review
        if updates:
            with journal_lock():
                current = read(record["id"])
                if not bound_record(current):
                    return
                for trigger in current["review"]["triggers"]:
                    url = updates.get(digest(trigger))
                    if url:
                        trigger["message_url"] = url
                current["review"]["binding"] = digest({k: v for k, v in current["review"].items() if k != "binding"})
                current["card_dirty"] = bool(current.get("delivery"))
                save(current)

    async def _flush_record(self, old, cfg):
        if not bound_record(old):
            return
        await self.source_links(old)
        old = read(old["id"])
        if old.get("status") == "applying" and old.get("delivery"):
            delivery = old["delivery"]
            old = decide(old["id"], old["review"]["binding"], "approve",
                         cfg["owner_id"], cfg["team_id"], cfg["channel_id"], delivery["ts"])
        rec = None
        with journal_lock():
            current = read(old["id"])
            if not bound_record(current):
                return
            if current.get("status", "Pending") in {"Pending", "Deferred"}:
                from tools.memory_tool import load_on_disk_store
                store = load_on_disk_store()
                review = current["review"]
                path = store._path_for(review["target"])
                with store._file_lock(path):
                    raw, ok = read_exact(store, path)
                    if not ok:
                        current.update(status="Failed", error="Memory unreadable; no approval offered", card_dirty=True)
                    elif raw != review["before_raw"]:
                        current.update(status="Stale", error="Target changed; stage a new proposal", card_dirty=True)
                    elif len(review["after_raw"]) > store._char_limit(review["target"]):
                        current.update(status="Failed", error="Current budget exceeded", card_dirty=True)
                save(current)
            if current.get("delivery"):
                if current.get("card_dirty"):
                    rec = current
            elif current.get("status", "Pending") in {"Pending", "Stale", "Failed"} and not current.get("sending"):
                current["sending"] = True
                save(current)  # at-most-once claim; uncertain send recovers via /memory
                rec = current
        if not rec:
            return
        if rec.get("delivery"):
            await self.update_card(rec)
            return
        blocks = render(rec)
        response = await self.client.chat_postMessage(channel=cfg["channel_id"],
            text="Private memory review pending", blocks=blocks,
            client_msg_id=rec["notification_id"], unfurl_links=False, unfurl_media=False)
        if not response.get("ok") or response.get("channel") != cfg["channel_id"] or not response.get("ts"):
            raise RuntimeError("Memory review delivery failed; recover using /memory")
        with journal_lock():
            rec = read(rec["id"])
            rec["delivery"] = {"ts": response["ts"], "channel": cfg["channel_id"],
                               "team": cfg["team_id"], "owner": cfg["owner_id"]}
            rec["card_dirty"] = True
            save(rec)
        if not await self.verify_card(rec):
            raise RuntimeError("Memory review delivery readback failed")
        with journal_lock():
            rec = read(rec["id"])
            rec["card_dirty"] = False
            save(rec)

    async def callback(self, body):
        if not await self.verify_private():
            raise PermissionError("Private destination not verified")
        item = body["actions"][0]
        pid, binding = item["value"].split(":", 1)
        action = item["action_id"].removeprefix("hermes_memory_")
        current = read(pid)
        if not current or "review" not in current:
            raise PermissionError("Unknown proposal")
        if not await self.verify_card(current):
            raise PermissionError("Card no longer matches the complete reviewed state")
        rec = decide(pid, binding, action, (body.get("user") or {}).get("id"),
                     (body.get("team") or {}).get("id"), (body.get("channel") or {}).get("id"),
                     (body.get("message") or {}).get("ts"))
        await self.update_card(rec)
        return rec["status"]


async def recover_command(service, actor, team, channel, args):
    """On-demand private recovery without trusting the originating thread."""
    cfg = settings()
    if (actor, team, channel) != (cfg.get("owner_id"), cfg.get("team_id"), cfg.get("channel_id")):
        return "Use the configured private memory review DM."
    if not await service.verify_private():
        return "Private review destination could not be verified."
    from tools import write_approval as wa
    if args and args[0] == "review":
        rec = read(args[1]) if len(args) == 2 else None
        if not bound_record(rec):
            return "No managed memory proposal with that ID."
        return review_text(rec)
    if args and args[0] == "recover" and len(args) == 2:
        with journal_lock():
            rec = read(args[1])
            if not bound_record(rec):
                return "No managed memory proposal with that ID."
            if not rec.get("delivery") and rec.get("status") in {"Pending", "Deferred", "Stale", "Failed"}:
                # Explicit owner retry only; never auto-resend an uncertain send.
                # The same client_msg_id lets Slack deduplicate a lost receipt.
                rec["sending"] = False
                save(rec)
    if args and args[0] not in {"pending", "recover"}:
        return "Use /memory, /memory pending, /memory review <id>; decide using the private card."
    await service.flush()
    records = wa.list_pending("memory", include_decided=True)
    lines = ["Private memory reviews:"]
    for rec in records:
        if bound_record(rec):
            extra = " — delivery uncertain; local review available" if rec.get("sending") and not rec.get("delivery") else ""
            lines.append(f"{rec['id']}: {rec.get('status', 'Pending')}{extra}")
    return "\n".join(lines) if len(lines) > 1 else "No memory reviews."
