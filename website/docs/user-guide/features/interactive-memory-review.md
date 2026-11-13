# Interactive private memory review

Interactive review is opt-in per profile. It uses the existing memory tool and
approval gate. The stable memory schema includes optional `source_refs`; it is not
changed mid-session, and no system prompt reload is added.
All agents use the same implementation under their own `HERMES_HOME`.

## Prepare without activating

Run `python scripts/memory_review_prepare.py` with the intended profile's
`HERMES_HOME`. This is an offline, read-only report. It does not change gates,
configure Slack, send messages, or restart a gateway. It cannot certify a DM.

Defaults in each profile:

```yaml
memory:
  write_approval: false
  review:
    enabled: false
    owner_id: ""
    team_id: ""
    channel_id: ""
```

After explicit authorization, configure these settings using `hermes -p PROFILE
config set memory.review.KEY VALUE`. The gate must separately be enabled with
`memory.write_approval: true`. Enabling review alone does not enable the gate.
Do not change any profile or restart any service as part of preparation.

The configured Slack workspace must already be connected. There is deliberately
no fallback to the primary workspace. Before any delivery or decision, Slack
`auth.test`, `conversations.info`, and `conversations.members` must establish the
exact workspace, a genuine one-to-one IM with the owner, and exactly the owner
and this bot as members. Private channels, public channels, group DMs and unknown
workspaces are not accepted. The app needs the corresponding read scopes plus
`chat:write` and `im:history` for card readback. Missing scopes fail closed.
Buttons are handled only by the authenticated Bolt/Socket Mode callback path.

## Review

Each proposal is persisted before an immediate private notification is scheduled,
including writes from the background review fork. The notification is independent
of the conversation's turn-end callback and never follows the originating shared
thread. A five-second outbox recovery loop resumes undispatched work on startup.
Deferred proposals are not repeatedly notified.

Cards include complete A/B entries, sequential batch operations, text differences,
profile/store/ID, character budget, trigger context, and a separate rationale
field. Replace changes the **whole selected entry**, not the matching substring.
Remove displays `Entry deleted`. Raw evidence is rendered as plain text, so it
cannot inject mentions or card markup.

Ingress captures session, author, workspace, source channel, thread, message ID,
timestamp and a bounded excerpt. Per-session snapshots persist under
`pending/memory_sources/` with content-addressed IDs. The new user-turn API sidecar
lists IDs/excerpts; past messages, toolsets and the cached system prompt remain
unchanged. Both main tool executors and the real background fork resolve
`source_refs` only against that session's validated runtime snapshots. The fork
freezes its catalog at spawn, not when its worker eventually runs. Catalog paths,
catalog envelopes and each content-addressed snapshot bind the exact profile home,
owner, workspace and private destination. Both model injection and attribution
revalidate that scope, including attribution while review is disabled. A copied
profile or changed destination cannot reuse historical sources; legacy unbound
catalogs/snapshots are not migrated or silently relabeled. Stale ingress context
also fails closed rather than becoming fallback evidence for another owner.
Rejected, deliberately empty, or unbound ingress never falls back to raw turn
text. Ingress-free CLI conversations still run and stage proposals, but raw text
without independently bound provenance is unavailable evidence, not an attributable
snapshot. Explicitly empty background captures stay empty even if the parent
later acquires sources; they do not default to the parent's live catalog.

Source selection is model attribution, **not independent proof of relevance**.
Cards say so. Missing/invalid references show `Review initiation context; relevance
unverified`, not trigger evidence. Synthetic review instructions never become
user evidence. Classification is conservatively inferred, not asserted to be an
explicit correction. Model rationale remains unavailable. Source links come only
from Slack `chat.getPermalink` on the exact authenticated configured workspace
client and captured source channel/message. Missing scopes, cross-workspace
sources or failed lookups leave links unavailable; no hostname is guessed.
Identical proposals retain all contributing triggers; adding a trigger changes
the revision and invalidates older button values until the card is refreshed.

Preflight rejects ambiguous/missing selectors, exact duplicates, conflicting
pending edits of the same entry, threats, malformed entry delimiters, unreadable
files, parser drift, and final character-budget overflow. Semantic contradictions
and the meaning of removed facts still require owner review: the card discloses
all prior text and says that no model assessment is available. It does not claim
to have verified semantic consistency.

Approve/Reject/Defer bind profile, workspace, owner, IM, message ID and exact
review revision. Approve compares exact disk bytes under the memory mutation lock,
then atomically writes and reads back the reviewed poststate. It never replays
substring selectors. A target change produces Stale; nothing is applied. Submit
a new proposal against current memory and review its new A/B before approving.

Applied/Rejected/Deferred/Stale/Failed outcomes remain in the durable journal.
Repeated decisions are idempotent. A write-ahead `applying` record reconciles a
crash or transient readback failure after memory commit without replaying the
operation. Uncertain outcomes remain `applying` until disk reconciliation; they
are not falsely labeled Failed/nothing applied. Failed card edits stay dirty,
retain retry diagnostics, and cannot block delivery of later proposals; success
is not claimed before readback. Existing session
prompt snapshots remain unchanged; memory takes effect in the next session.

## Recover

In the configured private owner DM:

- `/memory` or `/memory pending`: dispatch missing cards and list durable IDs/status.
- `/memory review ID`: inspect the full review.
- `/memory recover ID`: explicitly retry an uncertain delivery with the same
  Slack `client_msg_id`. This may duplicate a card if Slack's deduplication window
  has expired; it is never retried automatically after an uncertain send.
  This also recovers undelivered Stale/Failed cards, without approval buttons.

A crash after claiming delivery but before receiving Slack's receipt intentionally
favors no automatic spam over exactly-once notification. The private recover
command is the escape hatch. Review, recovery and listing enforce the persisted
profile/destination binding, so changing owner settings cannot disclose older
private proposals. A duplicate/obsolete card cannot authorize a different
proposal or target.

Local CLI `/memory review ID` also shows the full review. Legacy text
approve/reject refuses managed proposals rather than bypassing owner/card binding.
Legacy proposals are not silently migrated: their original before-state is
unknown, so restage them from current memory with unavailable historical evidence.
Cards exceeding Slack's block capacity are never truncated into an approvable
card; use local review and restage a smaller proposal. Approval still requires a
complete, bound private card.

## Canary and fleet rollout

The implementation is profile-generic but **not activated by shipping defaults**.
Before fleet activation, authorize one named profile, configure its verified owner
DM, enable the gate/review deliberately, and exercise staged background write →
private notification → owner Approve → exact memory readback → Applied card.
Also verify wrong-actor refusal, stale target, restart recovery, Reject and Defer.
Only then repeat explicit preparation/configuration for each intended profile.
No live canary, messages, profile changes, or restart were performed by the tests.

Tests use real temporary `HERMES_HOME` memory/config/proposal files and the real
memory staging, renderer, decision and readback code. Slack network operations and
the background fork's model output are offline test boundaries.
