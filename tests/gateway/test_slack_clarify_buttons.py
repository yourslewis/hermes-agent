"""Tests for Slack Block Kit interactive clarify buttons.

Mirrors test_slack_approval_buttons.py (harness) and
test_telegram_clarify_buttons.py (semantics) for the ``send_clarify`` override
and the indexed ``hermes_clarify_choice_<idx>`` /
``hermes_clarify_other`` action dispatch.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Slack SDK mock so SlackAdapter can be imported (mirrors
# test_slack_approval_buttons.py)
# ---------------------------------------------------------------------------
def _ensure_slack_mock():
    if "slack_bolt" in sys.modules:
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    sys.modules["slack_bolt"] = slack_bolt
    sys.modules["slack_bolt.async_app"] = slack_bolt.async_app
    handler_mod = MagicMock()
    handler_mod.AsyncSocketModeHandler = MagicMock
    sys.modules["slack_bolt.adapter"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode.async_handler"] = handler_mod
    sdk_mod = MagicMock()
    sdk_mod.web = MagicMock()
    sdk_mod.web.async_client = MagicMock()
    sdk_mod.web.async_client.AsyncWebClient = MagicMock
    sys.modules["slack_sdk"] = sdk_mod
    sys.modules["slack_sdk.web"] = sdk_mod.web
    sys.modules["slack_sdk.web.async_client"] = sdk_mod.web.async_client


_ensure_slack_mock()

from plugins.platforms.slack.adapter import SlackAdapter
from gateway.config import PlatformConfig


def _make_adapter():
    config = PlatformConfig(enabled=True, token="xoxb-test-token")
    adapter = SlackAdapter(config)
    adapter._app = MagicMock()
    adapter._bot_user_id = "U_BOT"
    adapter._team_clients = {"T1": AsyncMock()}
    adapter._team_bot_user_ids = {"T1": "U_BOT"}
    adapter._channel_team = {"C1": "T1"}
    return adapter


class _AuthRunner:
    def __init__(self, auth_fn=None):
        self._auth_fn = auth_fn or (lambda _source: True)

    async def handle(self, event):
        return None

    def _is_user_authorized(self, source):
        return self._auth_fn(source)


def _attach_auth_runner(adapter, auth_fn=None):
    adapter.set_message_handler(_AuthRunner(auth_fn=auth_fn).handle)


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


# ===========================================================================
# send_clarify — Block Kit render (a)
# ===========================================================================

class TestSlackSendClarify:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_multi_choice_renders_buttons_and_other(self):
        adapter = _make_adapter()
        mock_client = adapter._team_clients["T1"]
        mock_client.chat_postMessage = AsyncMock(return_value={"ts": "1234.5678"})

        result = await adapter.send_clarify(
            chat_id="C1",
            question="Which environment?",
            choices=["staging", "production"],
            clarify_id="cid1",
            session_key="sk1",
        )

        assert result.success is True
        assert result.message_id == "1234.5678"
        # ts recorded for the double-click guard
        assert adapter._clarify_resolved.get("1234.5678") is False

        kwargs = mock_client.chat_postMessage.call_args[1]
        blocks = kwargs["blocks"]
        assert blocks[0]["type"] == "section"
        assert "Which environment?" in blocks[0]["text"]["text"]
        option_blocks = [b for b in blocks[1:] if b["type"] == "section"]
        assert [b["text"]["text"] for b in option_blocks] == ["A. staging", "B. production"]
        elements = next(b["elements"] for b in blocks if b["type"] == "actions")
        # 2 choices + Other
        assert len(elements) == 3
        assert elements[0]["action_id"] == "hermes_clarify_choice_0"
        assert elements[0]["value"] == "cid1|0"
        assert elements[1]["action_id"] == "hermes_clarify_choice_1"
        assert elements[1]["value"] == "cid1|1"
        assert elements[0]["text"]["text"] == "Choose A"
        assert elements[1]["text"]["text"] == "Choose B"
        assert "A. staging" in kwargs["text"]
        assert "B. production" in kwargs["text"]
        # Final button is the free-text "Other"
        assert elements[2]["action_id"] == "hermes_clarify_other"
        assert elements[2]["value"] == "cid1|other"
        for block in blocks:
            if block["type"] == "actions":
                action_ids = [element["action_id"] for element in block["elements"]]
                assert len(action_ids) == len(set(action_ids))


    @pytest.mark.asyncio
    async def test_long_options_wrap_without_loss_and_click_returns_full_text(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        _attach_auth_runner(adapter)
        client = adapter._team_clients["T1"]
        client.chat_postMessage = AsyncMock(return_value={"ts": "long.1"})
        choices = ["Read local files without changes. " * 110,
                   "Keep <literal> & *all* details, including the final sentence."]
        cm.register("long", "sk-long", "Choose a scope", choices)
        result = await adapter.send_clarify(
            chat_id="C1", question="Choose a scope", choices=choices,
            clarify_id="long", session_key="sk-long", metadata={"thread_id": "thread.1"},
        )
        assert result.success
        sent = client.chat_postMessage.call_args.kwargs
        sections = [b for b in sent["blocks"][1:] if b["type"] == "section"]
        assert all(len(b["text"]["text"]) <= 3000 for b in sections)
        assert all(b["text"]["type"] == "plain_text" for b in sections)
        assert all(b.get("expand") is True for b in sections)
        assert "".join(b["text"]["text"] for b in sections) == (
            "A. " + choices[0].strip() + "B. " + choices[1]
        )
        assert choices[0].strip() in sent["text"]
        assert "&lt;literal&gt; &amp;" in sent["text"]
        assert sent["mrkdwn"] is False
        assert sent["unfurl_links"] is False
        assert sent["unfurl_media"] is False
        assert sent["thread_ts"] == "thread.1"
        body = {
            "message": {"ts": "long.1", "blocks": sent["blocks"]},
            "channel": {"id": "C1"}, "user": {"id": "U_N", "name": "norbert"},
        }
        await adapter._handle_clarify_action(
            AsyncMock(), body, {"action_id": "hermes_clarify_choice_0", "value": "long|0"},
        )
        entry = cm._entries["long"]
        assert entry.response == choices[0]
        updated = client.chat_update.call_args.kwargs
        assert "Choose a scope" in updated["text"]
        assert choices[0] in updated["text"]
        assert not any(b["type"] == "actions" for b in updated["blocks"])
        assert all(len(b["text"]["text"]) <= 3000
                   for b in updated["blocks"] if b["type"] == "section")
        assert choices[0] in "".join(
            b["text"]["text"] for b in updated["blocks"][1:]
        )

    @pytest.mark.asyncio
    async def test_oversized_options_fail_explicitly_instead_of_sending_invalid_blocks(self):
        adapter = _make_adapter()
        client = adapter._team_clients["T1"]
        result = await adapter.send_clarify(
            chat_id="C1", question="Pick", choices=["x" * 150000],
            clarify_id="too-long", session_key="sk-long",
        )
        assert not result.success
        assert "too long" in (result.error or "").lower()
        client.chat_postMessage.assert_not_called()

    @pytest.mark.asyncio
    async def test_mrkdwn_escapes_question(self):
        adapter = _make_adapter()
        mock_client = adapter._team_clients["T1"]
        mock_client.chat_postMessage = AsyncMock(return_value={"ts": "1.1"})

        await adapter.send_clarify(
            chat_id="C1",
            question="Use <A> & <B>?",
            choices=["yes"],
            clarify_id="cid2",
            session_key="sk2",
        )
        section_text = mock_client.chat_postMessage.call_args[1]["blocks"][0]["text"]["text"]
        assert "<A>" not in section_text
        assert "&lt;A&gt;" in section_text
        assert "&amp;" in section_text


# ===========================================================================
# _handle_clarify_action — choice click resolves (b)
# ===========================================================================

class TestSlackClarifyChoiceAction:
    def setup_method(self):
        _clear_clarify_state()


    @pytest.mark.asyncio
    async def test_unauthorized_click_ignored(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        _attach_auth_runner(adapter, auth_fn=lambda _s: False)
        cm.register("cidAuth", "sk-auth", "Pick", ["a", "b"])
        adapter._clarify_resolved["2.2"] = False

        ack = AsyncMock()
        body = {
            "message": {"ts": "2.2", "blocks": []},
            "channel": {"id": "C1"},
            "user": {"name": "mallory", "id": "U_BAD"},
        }
        action = {"action_id": "hermes_clarify_choice", "value": "cidAuth|0"}

        await adapter._handle_clarify_action(ack, body, action)

        with cm._lock:
            entry = cm._entries.get("cidAuth")
        assert entry is not None
        assert not entry.event.is_set()


# ===========================================================================
# _handle_clarify_action — "Other" → text-capture → typed reply (c)
# ===========================================================================

class TestSlackClarifyOtherFlow:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_other_flips_to_text_mode_then_typed_reply_resolves(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        _attach_auth_runner(adapter)
        cm.register("cidO", "sk-other", "Pick", ["x", "y"])
        adapter._clarify_resolved["4.4"] = False

        mock_client = adapter._team_clients["T1"]
        mock_client.chat_update = AsyncMock()

        ack = AsyncMock()
        body = {
            "message": {"ts": "4.4", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "❓ Pick"}},
                {"type": "actions", "elements": []},
            ]},
            "channel": {"id": "C1"},
            "user": {"name": "norbert", "id": "U_N"},
        }
        action = {"action_id": "hermes_clarify_other", "value": "cidO|other"}

        await adapter._handle_clarify_action(ack, body, action)

        # Entry flipped to text-capture; NOT yet resolved.
        pending = cm.get_pending_for_session("sk-other")
        assert pending is not None and pending.clarify_id == "cidO"
        assert pending.awaiting_text is True
        with cm._lock:
            entry = cm._entries.get("cidO")
        assert not entry.event.is_set()
        assert "awaiting" in mock_client.chat_update.call_args[1]["text"].lower()

        # Now the gateway text-intercept (platform-agnostic) resolves from the
        # user's next typed message. We exercise that leveraged path directly.
        assert cm.resolve_text_response_for_session("sk-other", "my custom answer") is True
        with cm._lock:
            entry = cm._entries.get("cidO")
        assert entry.response == "my custom answer"
        assert entry.event.is_set()


# ===========================================================================
# Base text-fallback unchanged for platforms without an override (e)
# ===========================================================================

class TestBaseAdapterClarifyFallbackUnchanged:
    @pytest.mark.asyncio
    async def test_base_numbered_text_fallback(self):
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        class _Stub(BasePlatformAdapter):
            name = "stub"

            def __init__(self):
                self.sent: list = []

            async def connect(self, *, is_reconnect: bool = False): pass
            async def disconnect(self): pass
            async def send(self, chat_id, content, **kw):
                self.sent.append(content)
                return SendResult(success=True, message_id="1")
            async def edit(self, *a, **k): return SendResult(success=False)
            async def get_history(self, *a, **k): return []
            async def get_chat_info(self, *a, **k): return {}

        adapter = _Stub()
        result = await adapter.send_clarify(
            chat_id="c", question="Pick a fruit",
            choices=["apple", "banana"], clarify_id="x", session_key="s",
        )
        assert result.success is True
        text = adapter.sent[0]
        assert "Pick a fruit" in text
        assert "1." in text and "apple" in text
        assert "2." in text and "banana" in text
