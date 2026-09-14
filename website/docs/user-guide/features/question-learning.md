# Editable question bank and Rex learning

The canonical bank can live in Notes under `03-Question-Bank/`. Each question is a Markdown note; `Guidance.md` holds interview guidance. New interviews validate and snapshot active questions. Existing interviews retain their saved snapshot and answers.

## Editing in Notes

Use **Question Bank → New question** or select an existing note. Edit its question, applicability and skip conditions. Required sections are `Question`, `Ask when`, `Skip when`, and `Resolves`. YAML frontmatter contains a stable `id`, `category`, `priority`, `status`, and `tags`; retain the ID and matching filename.

- `active`: available to new interviews.
- `proposed`: awaiting owner review; change to active to approve.
- `rejected`: retained for inspection, not asked.
- `auto-generated`: provenance tag on learned questions, including approved proposals. Manual edits do not remove it automatically.

Status and provenance filters help review additions. Saves require the SHA256 of the version opened. Conflicts preserve unsaved editor text and recoverable disk content instead of silently overwriting.

## Learning scope

Learning is opt-in through a profile-owned `question-learning.json`. The initial implementation admits only new canonical Slack messages from the configured Rex owner, team and channels. Bots, synthetic events, missing event authors, quoted/fenced text, other profiles and timestamps at or before activation are excluded. No historical mining occurs.

A bounded, no-tools primary-model call generalizes relevant corrections. Explicit corrections can become active questions; less explicit candidates remain proposals. Conservative validation may reject candidates rather than store sensitive material. Automatically learned entries are tagged `auto-generated`. The journal stores hashes and generalized question payloads, not raw conversation messages.

The learner only appends question notes. It cannot grant interview read access, approve planning, run commands, or execute a task. Replays are deduplicated, and existing/manual notes are never automatically merged or overwritten.

## Controls

In an admitted Slack conversation:

```
!interview learning status
!interview learning pause
!interview learning enable
!interview learning digest
!interview learning undo QUESTION_ID
```

Pause stops learning, but the Notes bank remains selected. Enable admits only messages newer than the new activation timestamp. Undo only operates on journal-owned additions, retains recovery content, and refuses to overwrite manual changes. Proposals are approved/rejected by editing status in Notes.

Digests are batched daily and delivered on your next admitted Rex message; they are silent when there are no changes. Delivery is acknowledged only after Slack reports success. A crash before acknowledgement can repeat a digest rather than lose it. The `digest` command previews changes immediately. Exact-time scheduled delivery is not configured.

## Configuration

Configuration belongs to the target profile, not the installed source tree:

```json
{
  "enabled": true,
  "profile": "rex",
  "owner": "OWNER_SLACK_ID",
  "team": "SLACK_TEAM_ID",
  "channels": ["OPTED_IN_CHANNEL_ID"],
  "since": "ACTIVATION_UNIX_TIMESTAMP",
  "notes_root": "/absolute/path/to/notes/03-Question-Bank",
  "cache_path": "/absolute/path/to/rex/question-bank-cache.json"
}
```

Do not copy example placeholders into a live configuration. An absent file preserves legacy bank selection. When the file exists, Notes selection is independent of `enabled`; this flag only controls learning. An explicitly configured cache enables last-good fallback for malformed manual edits; without one, invalid content fails closed. Never use a symlink to relocate the bank.

The Notes service edits the same directory as the loader and learner. Deploying only the UI or only the loader is not sufficient. Source-message identities and the learning journal remain outside the visible Notes directory. Notes' existing authentication and private delivery path must remain in place.
