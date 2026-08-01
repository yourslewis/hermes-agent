from __future__ import annotations

import json
import os
import urllib.request
from typing import Awaitable, Callable, Any

from .controller import HarnessController


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def post_slack_response(body: dict, text: str, *, replace_original: bool = False) -> bool:
    """Best-effort visible Slack response for button callbacks.

    Plugin Slack action handlers only receive ``ack, body, action``. Slack's
    ``response_url`` is the narrow callback-safe surface available here without
    reaching into the adapter internals. Tests and non-Slack paths may omit it;
    in that case this is a no-op and returns False.
    """
    response_url = (body or {}).get("response_url")
    if not isinstance(response_url, str) or not response_url:
        return False
    payload = {
        "text": text,
        "replace_original": replace_original,
        "response_type": "in_channel",
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        response_url,
        data=data,
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= int(resp.status) < 300
    except Exception:
        return False


def build_slack_message_payload(body: dict, text: str) -> dict | None:
    """Return a thread-preserving ``chat.postMessage`` payload for callbacks."""
    channel_id = ((body or {}).get("channel") or {}).get("id")
    message = (body or {}).get("message") or {}
    message_ts = message.get("ts")
    thread_ts = message.get("thread_ts") or ((body or {}).get("container") or {}).get("thread_ts") or message_ts
    if not channel_id:
        return None
    return {
        "channel": channel_id,
        "text": text,
        **({"thread_ts": thread_ts} if thread_ts else {}),
    }


def build_slack_ack_payloads(body: dict, text: str) -> tuple[dict | None, dict | None]:
    """Return ``(chat.update payload, chat.postMessage payload)`` for a button ack.

    Updating removes the stale action buttons; posting a thread reply makes the
    state transition visible even on clients that don't visually change a
    clicked button.
    """
    channel_id = ((body or {}).get("channel") or {}).get("id")
    message = (body or {}).get("message") or {}
    message_ts = message.get("ts")
    update_payload = None
    if channel_id and message_ts:
        blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            }
        ]
        update_payload = {
            "channel": channel_id,
            "ts": message_ts,
            "text": text,
            "blocks": blocks,
        }
    post_payload = build_slack_message_payload(body, text)
    return update_payload, post_payload


def _slack_api(method: str, payload: dict) -> bool:
    token = os.getenv("SLACK_CHLOE_BOT") or os.getenv("SLACK_BOT_TOKEN") or os.getenv("SLACK_BOT")
    if not token:
        return False
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://slack.com/api/{method}",
        data=data,
        headers={
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        return bool(parsed.get("ok"))
    except Exception:
        return False


def acknowledge_slack_action(body: dict, text: str) -> bool:
    """Make a button-click state transition visible in Slack.

    Prefer Web API update+reply because response_url is not always present in
    Socket Mode block_actions payloads. Fall back to response_url when token or
    message metadata is unavailable.
    """
    update_payload, post_payload = build_slack_ack_payloads(body, text)
    changed = False
    if update_payload:
        changed = _slack_api("chat.update", update_payload) or changed
    if post_payload:
        changed = _slack_api("chat.postMessage", post_payload) or changed
    if changed:
        return True
    return post_slack_response(body, text, replace_original=False)


def post_slack_thread_message(body: dict, text: str) -> bool:
    payload = build_slack_message_payload(body, text)
    if payload and _slack_api("chat.postMessage", payload):
        return True
    return post_slack_response(body, text, replace_original=False)


async def handle_approve_auto(
    *,
    ack: Callable[[], Awaitable[None]],
    body: dict,
    action: dict,
    controller: HarnessController,
    launch_auto: Callable[[str], Awaitable[None]],
    post_response: Callable[..., Any] | None = None,
) -> None:
    await ack()
    task_id = str(action.get("value") or "")
    actor = str(((body or {}).get("user") or {}).get("id") or "")
    result = controller.approve(task_id, actor=actor)
    if post_response:
        await _maybe_await(post_response(body, result.message))
    if result.changed:
        await launch_auto(task_id)


async def handle_cancel(
    *,
    ack: Callable[[], Awaitable[None]],
    body: dict,
    action: dict,
    controller: HarnessController,
    post_response: Callable[..., Any] | None = None,
) -> None:
    await ack()
    task_id = str(action.get("value") or "")
    actor = str(((body or {}).get("user") or {}).get("id") or "")
    result = controller.cancel(task_id, actor=actor)
    if post_response:
        await _maybe_await(post_response(body, result.message))
