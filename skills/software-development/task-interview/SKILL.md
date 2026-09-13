---
name: task-interview
description: Use when clarifying a task before any execution.
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [interview, requirements, clarification, slack]
    related_skills: [plan]
---

# Task Interview

## Overview

Turn an underspecified task into agreed requirements, not completed work. These
are starter questions, not learned user preferences or a mandatory questionnaire.
The gateway enforces the restricted interview lifecycle. This skill guides
question selection; it does not grant capabilities or replace runtime policy.
Loading this skill alone in ordinary chat does not activate interview mode.

## When to Use

- An owner starts `/interview <task>` in a Slack thread.
- An existing interview needs the next decision clarified or its summary revised.
- Outside interview mode, the user explicitly requests question-bank maintenance.

Do not use the interview to implement, deploy, send unrelated messages, invoke
other agents, or silently resume earlier execution. Planning is a separate,
explicitly approved phase and still does not authorize any implementation.

## Select the next question

1. **Read what is already known.** Use the task, answer ledger, and permitted
   context. Separate confirmed facts from assumptions. Do not search unrelated
   sessions or the web: v1 deliberately blocks both. Local reads are available
   only under explicitly approved roots. Done when the next unresolved decision
   can be named without requesting facts already supplied.
2. **Filter before ranking.** Consider common questions and only relevant domain
   categories (coding, research, or a curated custom category). Apply `skip_when`
   first. Apply `ask_when` to the remaining candidates, then use `resolves` to
   check whether an answer would materially change the deliverable, risk, scope,
   or acceptance test. Done when every candidate has a concrete reason to ask.
3. **Ask the highest-value remaining decision.** Priority breaks ties; it does
   not override a skip condition. Adapt wording to the user's task. Ask one
   question at a time, with zero to four neutral, distinct choices. Slack supplies
   Other/free text; do not bake an extra fifth option into the bank. Describe
   meaningful tradeoffs, not an obviously correct answer plus straw alternatives.
   Done when answering the question resolves one decision.
4. **Record without guessing.** Preserve the user's full answer. If they do not
   know, retain an explicit uncertainty or propose a labeled assumption rather
   than converting silence to approval. Do not repeatedly ask the same question.
   Done when confirmed decisions and open issues remain distinguishable.
5. **Stop when sufficient.** Do not exhaust the bank or aim for a fixed count.
   If the owner requests finish, summarize now and include unresolved issues.
   Done when the summary contains enough information for the next human decision,
   not when every possible question has been answered.

## Summary and optional plan

Summarize the objective, audience/use case, scope and non-goals, acceptance
criteria, constraints, decisions, assumptions, unresolved questions, and supplied
references. Omit irrelevant headings for small tasks. Do not claim verification
that did not happen or invent evidence unavailable in the restricted loop.

The gateway delivers the summary before its planning decision card. It offers
Create the plan, Revise requirements, and Stop here, plus Other. Only an explicit
Create the plan selection authorizes a plan. Silence, timeouts, Other, and “looks
good” do not authorize one. Do not add an implementation action or a second
planning prompt of your own.

If planning is approved, describe steps, proposed agent roles and ownership,
dependencies, safe parallel work, integration points, review gates, and acceptance
tests. Proposed multi-agent work is a description, not permission to spawn agents.
Return the plan in Slack; do not export a file. After the plan, stop. Completion
and planning remain restricted until the owner explicitly exits the mode.

## Bank files and schema

The loader includes this complete guidance, the complete
[good/bad examples](references/good-and-bad-examples.md), and validated entries
from all `references/*-questions.yaml` files in a content-addressed snapshot.
Starter files:

- [Common questions](references/common-questions.yaml): outcome, audience, scope,
  constraints, acceptance, dependencies, permissions, deliverable.
- [Coding questions](references/coding-questions.yaml): compatibility, test
  expectations, deployment target.
- [Research questions](references/research-questions.yaml): decision, evidence
  standard, freshness, output.

Each YAML document is a mapping with one key, `questions`, holding a nonempty
list. Each entry needs a globally unique `id`, `category`, `ask_when`, `skip_when`,
`question`, `resolves`, `priority` (`high`, `medium`, or `low`), and a nonempty
`examples` list of `{task, good, bad}` strings. `choices` is optional, with at
most four nonempty strings. Other fields are rejected. IDs are lowercase letters,
digits, underscores, or hyphens, start alphanumeric, and are at most 128 characters.
Categories may be extended; questions are selected for relevance, not by an
inflexible category-to-task mapping.

Use plain UTF-8 YAML, without aliases, object constructors, duplicate keys, or
excessive nesting. Required files must be nonempty regular files, not symlinks.
Limits are 100,000 bytes per file, 500,000 bytes combined, and 16 question files.
Keep references compact enough to include completely, rather than relying on
pagination or linked guidance that the loader does not include.

## Explicit maintenance, outside the mode

Learning is not automatic. Do not call skill or memory writers from an interview.
Do not mine conversations or store private answers as generic examples.

1. Receive an explicit maintenance request, correction, or supplied example in
   ordinary chat outside interview mode. Identify the intended bank and profile;
   do not install into a different live profile. Done when scope is explicit.
2. Propose a small diff containing the trigger, skip condition, question, resolved
   decision, priority, and a good/bad example. Redact private details unless their
   reuse is explicitly approved. Done when the user can review the actual change.
3. Apply only the approved diff outside mode and validate the whole bank. A custom
   `$HERMES_HOME/skills/task-interview` replaces, not merges with, shipped defaults.
   An invalid custom bank must fail entry, never quietly use starter questions.
   Done when schema, unique IDs, limits, and complete guidance validate.
4. Start a new interview to use the change. Existing interviews retain their
   original prompt and SHA256 version through resume; never rewrite cached
   guidance mid-interview. Done when the new snapshot reflects the change while
   the old snapshot remains unchanged.

## Common Pitfalls

- **Checklist interviewing:** asking audience or format after the user stated it.
  Evaluate skip conditions before priority, even for high-priority entries.
- **Permission inflation:** treating “yes” to a requirement as approval to deploy.
  Requirements and execution authorization are separate.
- **Research theater:** inventing live citations because web tools are unavailable.
  Ask for supplied evidence or label the research step as future work.
- **Implicit learning:** copying task answers into this skill. Maintenance needs
  its own request and approval outside the restricted interview.
- **Premature planning:** offering implementation steps before agreement on the
  requirements, or writing a plan file after a Slack-only planning choice.

## Verification Checklist

- [ ] Each asked question resolves a material, still-open decision.
- [ ] Known answers and skip conditions remove redundant questions.
- [ ] At most one question is outstanding, with at most four choices plus Other.
- [ ] Assumptions and unknowns are not reported as confirmed facts.
- [ ] Summary precedes the gateway-owned planning decision.
- [ ] No task changes, delegation, automatic learning, or automatic execution.
- [ ] Active interviews retain their loaded snapshot after bank maintenance.
