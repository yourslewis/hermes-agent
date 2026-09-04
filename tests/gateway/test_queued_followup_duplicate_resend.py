"""Regression tests for the "replies twice" duplicate in the queued follow-up
path of ``GatewayRunner._run_agent``.

Symptom (reported on Don / Chloe's long Slack threads): the bot posts the same
reply twice, back to back, as two near-identical messages — *not* two separate
turns. The transcript has zero back-to-back assistant messages, confirming the
duplicate is a single turn's reply delivered twice.

Mechanism: when a turn completes normally but a message is already queued for
the session (e.g. a background-process completion notification landed on the
turn boundary), the gateway must deliver the turn's first response *before*
recursing into the queued follow-up.  If the stream consumer's delivery cannot
be *confirmed* as the final content — the #71643 "stale finalize" case, where
the consumer did post a real message but its recorded payload is a stale
preview snapshot (``delivered_final_matches(final) is False``) — the legacy
code re-posted the first response via a fresh ``adapter.send()``.  Because the
consumer had *already* posted that same reply while streaming, the user sees
two near-identical messages.

Fix: the branch now calls ``_deliver_queued_followup_first_response``, which
prefers **editing the already-posted streamed message in place** up to the
complete response (the same pattern the normal completed-turn path uses for
stale-finalize / transformed responses) and only falls back to a fresh send
when no single editable message exists (``__no_edit__`` sentinel, or a
multi-message split delivery) or the edit fails.  The complete answer still
always reaches the user — we just stop the visible duplicate.

These tests drive the extracted helper directly with a *real*
``GatewayStreamConsumer`` placed in the exact state that reaches the branch,
so the attribute names and the tri-state ``delivered_final_matches`` verdict
exercised here match production.
"""

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import _deliver_queued_followup_first_response
from gateway.session import SessionSource
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


class _RecordingAdapter(BasePlatformAdapter):
    """Adapter that records sends and edits for assertions."""

    def __init__(self, edit_success: bool = True):
        super().__init__(PlatformConfig(enabled=True, token="fake"), Platform.DISCORD)
        self.edit_success = edit_success
        self.sent = []      # fresh sends: list of (chat_id, content, metadata)
        self.edits = []     # in-place edits: list of (chat_id, message_id, content, kwargs)

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id="fresh-1")

    async def send_typing(self, chat_id, metadata=None):
        pass

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.edits.append((chat_id, message_id, content, {"finalize": finalize, "metadata": metadata}))
        return SendResult(success=self.edit_success, message_id=message_id,
                          error=None if self.edit_success else "boom")


class _PlainEditAdapter(_RecordingAdapter):
    """Adapter whose edit_message has NO ``metadata`` parameter (base adapter
    signature).  Used to verify the inspect-based guard omits the kwarg."""

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.edits.append((chat_id, message_id, content, {"finalize": finalize}))
        return SendResult(success=True, message_id=message_id)


def _source(chat_id="C123"):
    return SessionSource(platform=Platform.DISCORD, chat_id=chat_id, chat_type="dm")


def _make_consumer(adapter, *, message_id, stale_preview=True, split=False,
                    final_sent=True):
    """A real GatewayStreamConsumer in the post-finalize state.

    ``message_id``: the real id of the message the consumer already posted
        while streaming (``None`` => nothing posted).
    ``stale_preview``: when True the recorded turn-final payload is a stale
        preview snapshot that DIFFERS from the completed response, so
        ``delivered_final_matches(final) is False`` — the #71643 case that
        makes the gateway's confirmation predicate return False and reach the
        resend branch.
    ``split``: multi-message split delivery (no single editable message).
    """
    consumer = GatewayStreamConsumer(
        adapter=adapter,
        chat_id="C123",
        config=StreamConsumerConfig(),
    )
    consumer._message_id = message_id
    consumer._final_response_sent = final_sent
    consumer._final_content_delivered = final_sent
    consumer._turn_split_delivery = split
    if message_id is not None:
        # Record a turn-final payload.  If stale, it is a short preview that
        # differs from the real final response; if not, it equals it.
        consumer._delivered_final_text = (
            "Let me check on that..." if stale_preview else "the final response text"
        )
    return consumer


def _branch_reached(consumer, final_response: str) -> bool:
    """Reproduce the gateway's gate for the resend branch: the turn is NOT
    interrupted, there IS a queued message, and the streamed delivery is not
    confirmed.  The confirmation logic is the local helper
    ``_stream_confirmed_final_delivery`` inside _run_agent_inner; it trusts
    ``final_response_sent`` unless ``delivered_final_matches(final) is False``.
    """
    if consumer is None:
        return not bool(final_response)
    if getattr(consumer, "final_response_sent", False):
        matcher = getattr(consumer, "delivered_final_matches", None)
        if callable(matcher):
            try:
                if matcher(final_response) is False:
                    return bool(final_response)  # not confirmed -> resend branch
            except Exception:
                pass
        return False
    return bool(final_response)


@pytest.mark.asyncio
async def test_stale_finalize_edits_in_place_no_duplicate():
    """The reported bug: consumer posted a real message but the recorded
    payload is a stale preview (delivered_final_matches is False).  The first
    response must be delivered by EDITING that message, NOT a fresh send, so
    the user sees one corrected message instead of two near-identical ones."""
    adapter = _RecordingAdapter(edit_success=True)
    final_response = (
        "Here is the full answer: the Cosmos job is at 82% and on track to "
        "finish by 03:40 UTC.  Disk usage is nominal and no retries are "
        "needed, so I will keep watching and report when it completes."
    )
    # Consumer already posted a message; its recorded payload is a stale
    # preview, so the completed reply is NOT yet on screen in full form.
    consumer = _make_consumer(adapter, message_id="stream-msg-1", stale_preview=True)

    # Sanity: this is precisely the state that reaches the resend branch.
    assert _branch_reached(consumer, final_response) is True, (
        "test precondition: the recorded payload must differ from the final "
        "response so the gateway cannot confirm streamed delivery"
    )

    edited = await _deliver_queued_followup_first_response(
        adapter, _source(), consumer, final_response,
        session_key="agent:main:discord:dm:C123",
    )

    # No duplicate post — the whole point of the fix.
    assert adapter.sent == [], (
        f"fresh send produced a visible duplicate: {adapter.sent}"
    )
    # The already-posted message was edited up to the complete response.
    assert len(adapter.edits) == 1
    (chat_id, message_id, content, kwargs) = adapter.edits[0]
    assert chat_id == "C123"
    assert message_id == "stream-msg-1"
    assert content == final_response
    assert kwargs["finalize"] is True
    assert kwargs["metadata"] is None  # no thread metadata passed in the test
    assert edited is True


@pytest.mark.asyncio
async def test_confirmed_final_streamed_delivery_is_untouched():
    """When the streamed delivery IS confirmed (recorded payload equals the
    final response), the resend branch is not reached at all; the helper is
    only called in the unconfirmed case.  Verify the precondition is False so
    the branch's guard is exercised correctly end to end."""
    adapter = _RecordingAdapter(edit_success=True)
    final_response = "the final response text"
    consumer = _make_consumer(adapter, message_id="stream-msg-1", stale_preview=False)
    # Force the recorded payload to equal the final response.
    consumer._delivered_final_text = final_response.strip()
    assert _branch_reached(consumer, final_response) is False, (
        "confirmed delivery must not reach the resend branch"
    )


@pytest.mark.asyncio
async def test_no_consumer_falls_back_to_fresh_send():
    """When there is no stream consumer (non-streaming path), behavior is
    unchanged: the first response is delivered via a fresh send."""
    adapter = _RecordingAdapter()
    final_response = "plain answer"
    edited = await _deliver_queued_followup_first_response(
        adapter, _source(), None, final_response,
        session_key="agent:main:discord:dm:C123",
    )
    assert edited is False
    assert len(adapter.sent) == 1
    assert adapter.sent[0][1] == final_response
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_split_delivery_falls_back_to_fresh_send():
    """A multi-message split delivery has no single editable message that can
    hold the whole answer, so we keep the legacy fresh send (no partial
    overwrite)."""
    adapter = _RecordingAdapter(edit_success=True)
    final_response = "a very long answer that was split across several messages"
    consumer = _make_consumer(adapter, message_id="stream-msg-1",
                              stale_preview=True, split=True)
    assert consumer._turn_split_delivery is True
    edited = await _deliver_queued_followup_first_response(
        adapter, _source(), consumer, final_response,
        session_key="agent:main:discord:dm:C123",
    )
    assert edited is False
    assert len(adapter.sent) == 1
    assert adapter.sent[0][1] == final_response
    # No partial in-place edit was attempted.
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_no_edit_sentinel_falls_back_to_fresh_send():
    """The ``__no_edit__`` sentinel means there is no message id to edit
    (e.g. a draft-only stream).  Fall back to the fresh send."""
    adapter = _RecordingAdapter(edit_success=True)
    final_response = "answer after a draft-only stream"
    consumer = _make_consumer(adapter, message_id="__no_edit__", stale_preview=True)
    edited = await _deliver_queued_followup_first_response(
        adapter, _source(), consumer, final_response,
        session_key="agent:main:discord:dm:C123",
    )
    assert edited is False
    assert len(adapter.sent) == 1
    assert adapter.edits == []


@pytest.mark.asyncio
async def test_edit_failure_falls_back_to_fresh_send():
    """If the in-place edit fails, the complete answer must still reach the
    user via the fresh send (never silently dropped)."""
    adapter = _RecordingAdapter(edit_success=False)
    final_response = "the answer, retried via a fresh send"
    consumer = _make_consumer(adapter, message_id="stream-msg-1", stale_preview=True)
    edited = await _deliver_queued_followup_first_response(
        adapter, _source(), consumer, final_response,
        session_key="agent:main:discord:dm:C123",
    )
    assert edited is False
    # The edit was attempted first...
    assert len(adapter.edits) == 1
    # ...then the fresh send delivered the complete response.
    assert len(adapter.sent) == 1
    assert adapter.sent[0][1] == final_response


@pytest.mark.asyncio
async def test_metadata_kwarg_only_when_supported():
    """The base adapter's edit_message has no ``metadata`` parameter; only
    adapters that declare one (e.g. Slack) receive it.  The inspect-based
    guard must not pass ``metadata`` to a plain adapter."""
    adapter = _PlainEditAdapter()
    final_response = "answer via a plain adapter"
    consumer = _make_consumer(adapter, message_id="stream-msg-1", stale_preview=True)
    edited = await _deliver_queued_followup_first_response(
        adapter, _source(), consumer, final_response,
        session_key="agent:main:discord:dm:C123",
    )
    assert edited is True
    (chat_id, message_id, content, kwargs) = adapter.edits[0]
    assert content == final_response
    # No "metadata" key was passed to the plain adapter.
    assert "metadata" not in kwargs
