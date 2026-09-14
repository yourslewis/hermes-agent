---
sidebar_position: 18
title: Interview Mode
---

# Interview Mode

Interview mode clarifies a task in a **Slack thread** before any execution. Hermes
asks one question at a time, records decisions, and produces a requirements
summary. You can then choose an optional implementation **plan**. Neither the
interview nor that plan authorizes Hermes to implement the task.

This is an opt-in, thread-local mode for a dedicated profile gateway. It is not a
global preference, an OS sandbox, or a restriction activated merely by loading the
`task-interview` skill. Multiplex-profile gateways are not supported in v1.

## Start and control an interview

Send the command inside the intended Slack thread:

```text
!interview Define a migration from our existing billing provider
```

In an existing thread, mention your bot and type the command as message text
(e.g. `@Rex !interview <task>`). Native Slack slash commands cannot run inside
threads. The `!` form avoids Slack treating your message as a native slash
command. Legacy `/interview` and `/hermes interview` message text is also
recognized. No standalone native slash command is registered, preserving Slack's
command-count limit; there is no Slack app-manifest update to perform.

| Command | Effect |
| --- | --- |
| `!interview <task>` | Start a restricted interview in the current thread. A task is required. |
| `!interview --project /absolute/project -- <task>` | Select a project and grant bounded reads when starting. `--read-root` is an alias. |
| `!interview approve-read /absolute/project` | Add an approved project to the current interview without restarting or losing answers. |
| `!interview approve-history SESSION_ID` | Approve a specific eligible prior session for this interview. |
| `!interview status` | Show phase, research capabilities, approved read roots, and history-session grants without a model call. |
| `!interview finish` | Request the requirements summary now, including unresolved items. |
| `!interview resume` | Reissue the saved unanswered card, retry saved output delivery, or resume clarification when no card is pending. |
| `!interview exit` | Leave the mode for subsequent requests. Nothing is executed by exiting. |

Only the initiating user may answer or control the interview. Workspace,
channel, thread, and owner identity bind the conversation and its buttons;
normal gateway authorization still applies. Other participants cannot take over
by clicking the card. Unrelated threads retain ordinary routing.

Starting in a thread with active execution is refused: entering interview mode
does not cancel an existing job. A bare `!interview` asks you to supply a task,
rather than guessing from unrelated history.

## Questions and answers

Questions use native Slack choices, with up to four options and **Other** for
free text. Full option descriptions appear in the card body; short choice buttons
avoid truncating the meaning. You can also type an answer in the interview thread.
There is only one outstanding question, and v1 is single-select, not multi-select.

Hermes should skip decisions already answered in the task, answer ledger, or
approved context. The starter bank is guidance, not a required questionnaire. If
you do not know an answer, say so: uncertainty belongs in the summary rather than
being replaced with invented agreement.

Old, duplicate, wrong-owner, or wrong-conversation button actions cannot advance
the interview. Waiting does not pin a worker indefinitely. State is durable;
restart, timeout, or failed delivery does not silently switch the thread back to
execution. If paused or a card is stale, use `!interview status`, then
`!interview resume` to continue. Controls such as `/stop`, `/reset`, and `/new`
pause the interview rather than remove its restrictions. Use explicit
`!interview exit` to leave.

## Research, permissions, and execution restrictions

The interview runs in a **restricted research loop**, not the ordinary
task-execution loop. Public **web search and page extraction are available by
default**, using credential-free DuckDuckGo HTML search with Bing RSS fallback
and public-only network checks (independent of the normal agent's configured web
backend). Provider blocks, challenges, and unavailable pages return explicit
errors. Hermes
can also search the **current interview's** saved context. Research may inform
questions and requirements; it never authorizes implementation. Interactive
browser automation and arbitrary tools remain unavailable.

### Select or approve a project

```text
!interview --project "/absolute/path/to/project" -- Clarify the reconnect fix
```

Explicit project selection grants bounded read/search access to that project by
default. Repeat `--project` or its `--read-root` alias before ` -- ` to select
multiple roots. Simply mentioning a path in task text **does not grant access**.
If the interview is already running, do not restart it:

```text
!interview approve-read "/absolute/path/to/project"
```

This preserves the task, question bank, messages, and recorded answers. Each root
must be an existing absolute directory; symlinks (including parent symlinks), `/`,
home directories, broad non-project containers, profile state, and protected
secret locations are rejected. Named projects under `.hermes/repos/` or
`.hermes/worktrees/` are eligible; `.hermes/profiles/` is not. Reads remain bounded
and cannot escape the approved roots or read protected credential files. Choose
the narrowest relevant directory. Selecting a project is not a guarantee that
all its contents are public; interview context is visible to the thread's members.

When Hermes needs an unapproved project it may use `request_read_access(path,
reason)`. This creates a **pending request**, not a grant. The owner receives a
card showing the exact directory, model-provided reason, and **Approve read
access / Deny read access** buttons. The request is saved before delivery and can
be reissued with `!interview resume` after a failure or restart. Only the owner
and exact current card in the bound conversation can approve it; approval
revalidates the directory. Other/free text, natural-language agreement, and
ordinary model-generated question choices never grant permissions.

### Approve history explicitly

```text
!interview approve-history SESSION_ID
```

Only explicitly approved, eligible sessions can be read/searched. Approval
validates the session against the interview's owner, active profile, Slack team,
and channel. Other threads in the same channel require explicit session grants;
other channels, DMs, profiles, and sessions with missing or contradictory source
provenance are denied. Knowing a session ID is not sufficient to access another user's or profile's
history. Rejected or unavailable sessions are not added to the grants. Existing
interviews retain their prompt snapshot and messages when a grant is added.
Use `!interview status` to inspect current grants. No approval is inferred from
the task, a model response, or a natural-language request.

During questioning, completion, and optional planning, the mode does not permit:

- Terminal commands, code execution, process control, or running tests.
- Task-file writes, patches, configuration changes, or plan-file export.
- Delegation, external coding harnesses, jobs, scheduling, or deployments.
- Skill/memory updates, automatic learning, or arbitrary plugin/deferred tools.
- Other execution or runtime-changing slash commands from the restricted thread.

“No task changes” still allows Hermes to persist its internal interview state and
messages, call the configured model, and post/update interview UI in the bound
Slack thread. It does not promise zero disk writes or zero network activity.

## Summary, optional plan, and stopping

The summary distinguishes confirmed decisions, labeled assumptions, and open
questions. Where relevant it includes the objective, audience, scope/non-goals,
acceptance criteria, constraints, dependencies, and supplied references.

**After delivering the summary**, Hermes offers:

- **Create the plan:** produce a multi-step, multi-agent implementation plan in
  Slack. Roles, ownership, dependencies, parallel work, review gates, and tests
  are proposed, not executed. No agents are spawned and no plan file is written.
- **Revise requirements:** return to restricted clarification.
- **Stop here:** stop without planning or execution.

Only an explicit **Create the plan** selection authorizes planning. “Looks good,”
Other/free text, no response, and a timeout do not. The decision is bound to the
owner and current summary/card; an old card cannot authorize a changed summary.
After a plan, Hermes stops without an automatic implementation offer.

The thread remains restricted after a summary or plan. Use `!interview resume`
for revisions, or `!interview exit` and then a **separate new request** if you want
task execution. Exiting does not execute the original task or resume an old job.

## Question banks

Hermes ships a starter bank at:

```text
skills/software-development/task-interview/
  SKILL.md
  references/common-questions.yaml
  references/coding-questions.yaml
  references/research-questions.yaml
  references/good-and-bad-examples.md
```

An existing `$HERMES_HOME/skills/task-interview/` takes precedence and replaces the
shipped bank completely. This is the active runtime/profile home, not necessarily
`~/.hermes`. The loader never installs files into a live profile. If the custom
bank exists but is invalid, entry fails clearly; Hermes does **not** silently fall
back to starter questions. With no custom bank, it reads the shipped starter.

A bank needs a valid `SKILL.md` (name `task-interview`, description, and nonempty
body), `references/good-and-bad-examples.md`, and at least one
`references/*-questions.yaml`. Files are UTF-8 and nonempty regular files, not
symlinks. Each YAML file has a nonempty `questions` list:

```yaml
questions:
  - id: common-success
    category: common
    priority: high
    ask_when: The outcome lacks a testable acceptance criterion.
    skip_when: A measurable definition of done is already supplied.
    question: What observable result would make this successful?
    choices: []
    resolves: Acceptance criteria and definition of done.
    examples:
      - task: Improve the reporting dashboard.
        good: Which comparison must a reviewer be able to make?
        bad: Can you give me more details?
```

All fields shown except `choices` are required. Text must be nonempty; `examples`
contains one or more `task`/`good`/`bad` mappings. IDs must be globally unique,
alphanumeric initially, and then lowercase letters, digits, hyphens, or underscores
(up to 128 characters). Priorities are `high`, `medium`, or `low`; categories can
be extended. Choices contain at most four strings; Slack supplies Other. Unknown
question/example fields, duplicate YAML keys, aliases, unsafe constructors, and
excessive nesting are rejected. Limits are **100,000 bytes per file**, **500,000
bytes combined**, and **16 question files**. The full guidance and examples are
included, not truncated to a skill description.

### Explicit maintenance only

Bank changes happen through a **separate maintenance request outside interview
mode**. Provide a correction or example, review the proposed question/trigger/
skip-condition diff, and explicitly approve saving it to the intended bank.
Do not reuse private task answers as generic examples without permission.

There is no automatic conversation mining, fine-tuning, memory refinement, or
bank writing. New interviews use the new bank; active interviews retain their
original full prompt and SHA256 version, including across resume. Editing a bank
does not rewrite an active interview's cached guidance.

From a repository checkout, validate a proposed custom bank without installing it:

```python
from agent.interview_questions import load_question_bank

bank = load_question_bank(home="/path/to/staged/hermes-home")
print(bank["version"])  # SHA256 of the exact full prompt snapshot
```

The directory supplied as `home` contains `skills/task-interview`, not the bank
files directly. The return value has `version`, `prompt`, and `questions`. The
loader performs no setup, installation, automatic learning, or maintenance writes.
