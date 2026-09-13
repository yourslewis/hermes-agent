# Good and bad interview examples

These are fictional starter examples, not retained answers or learned preferences.

## Skip facts already supplied

**Task:** “Write a one-page brief for finance leaders comparing two billing vendors.”

- **Bad:** “Who is the audience? What format do you want?”
- **Good:** “Which would change the recommendation most: integration cost, invoice
  accuracy, or international tax coverage?”
- **Why:** Audience and length are known; the decision criteria are not. If those
  criteria are already present too, skip this question as well.

## Use approved context without expanding access

**Task:** “Clarify a fix for this repository's reconnect behavior.” The owner has
approved a local repository root containing the manifest and existing tests.

- **Bad:** “What programming language is it?” or running a test suite during the
  interview to discover behavior.
- **Good:** Read the relevant manifest and test text through permitted local
  reads, then ask “Must the connection recover after a server restart, or only
  transient network drops?” if the approved context leaves this unresolved.
- **Why:** Context can eliminate a question. Read permission does not permit
  terminal commands, execution, or access outside the approved root.

## Offer neutral decisions, not leading choices

**Task:** “Help me define a reporting dashboard.”

- **Bad:** “Should it be useful and fast, or confusing and slow?”
- **Good:** “Which first-version use matters most?” with choices “Daily operations,”
  “Monthly financial review,” and “Investigating individual incidents.”
- **Why:** These lead to different scopes without pretending one is always best.
  Slack adds Other/free text; do not add a fifth bank choice.

## Keep uncertainty explicit

**Task:** “We might have to support older mobile devices, but I'm not sure.”

- **Bad:** “Confirmed: all legacy devices supported.”
- **Good:** “Open issue: minimum supported device version. Proposed assumption for
  review: current supported versions only; owner has not approved this.”
- **Why:** A question can end with an unresolved dependency rather than an invented
  answer. Never convert a timeout or missing response into consent.

## Finish without task execution

**Task:** “Finish the interview; leave the budget as an open question.”

- **Bad:** “I'll implement the cheapest option now.”
- **Good:** Summarize confirmed requirements and list budget as unresolved. The
  gateway then asks whether to create a plan, revise requirements, or stop.
- **Why:** Finish is a summary request, not approval to change the task. Even a
  subsequent Create the plan choice authorizes only a Slack plan, not file
  export, delegation, deployment, or implementation.

## Research without invented sources

**Task:** “Define a report on current battery technology.”

- **Bad:** “Recent papers prove this,” accompanied by made-up citations.
- **Good:** “What source standard and publication window should the later research
  use?” State that live web and history retrieval are unavailable in v1 interview
  mode; use owner-supplied evidence or record retrieval as future work.
- **Why:** Restricted access limits what can be verified, not the duty to say so.

## Maintain only with separate approval

**Correction:** “You asked for the audience after I had already told you.”

- **Bad:** Write the conversation into the bank or memory during the interview.
- **Good:** Apply the correction to this interview. Outside mode, if the user
  requests a reusable change, propose a generic skip-condition diff and ask for
  approval before saving it.
- **Why:** Learning is not automatic. Existing interviews keep their frozen bank;
  future interviews can use an explicitly approved revision.
