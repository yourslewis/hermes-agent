from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .controller import DecisionLogEntry, HarnessTask
from .parser import ParsedHarnessOutput, ParsedQuestion


class HarnessTaskStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def task_dir(self, task_id: str) -> Path:
        return self.root / task_id

    def save(self, task: HarnessTask) -> None:
        d = self.task_dir(task.task_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "task.json").write_text(json.dumps(_task_to_dict(task), indent=2, sort_keys=True), encoding="utf-8")
        (d / "state.md").write_text(render_task_markdown(task), encoding="utf-8")

    def load(self, task_id: str) -> HarnessTask:
        data = json.loads((self.task_dir(task_id) / "task.json").read_text(encoding="utf-8"))
        return _task_from_dict(data)


def _task_to_dict(task: HarnessTask) -> dict:
    data = asdict(task)
    return data


def _task_from_dict(data: dict) -> HarnessTask:
    data = dict(data)
    data["decision_log"] = [DecisionLogEntry(**entry) for entry in data.get("decision_log", [])]
    return HarnessTask(**data)


def render_task_markdown(task: HarnessTask) -> str:
    lines = [
        f"# Harness Task {task.task_id}",
        "",
        "## Objective",
        task.goal,
        "",
        "## Current State",
        f"- State: {task.state}",
        f"- Harness: {task.harness}",
        f"- Model: {task.model}",
        f"- Can stop: {task.can_stop}",
        "",
        "## Plan",
        task.plan_text or "(none)",
        "",
        "## Last Harness Output",
        task.last_harness_output or "(none)",
        "",
        "## Evidence",
        *(task.evidence or ["(none)"]),
        "",
        "## Actions",
        *(task.actions or ["(none)"]),
        "",
        "## Open Questions",
    ]
    if task.open_questions:
        for q in task.open_questions:
            lines.append(f"- {q.get('id')}: {q.get('question')} status={q.get('status', 'open')}")
    else:
        lines.append("(none)")
    lines.extend(["", "## Decision Log"])
    if task.decision_log:
        for entry in task.decision_log:
            lines.extend([
                f"### {entry.time}",
                f"- Signal: {entry.signal}",
                f"- Evidence: {entry.evidence}",
                f"- Decision: {entry.decision}",
                f"- Expected outcome: {entry.expected_outcome}",
                f"- Action sent: {entry.action_sent}",
                "",
            ])
    else:
        lines.append("(none)")
    return "\n".join(lines).rstrip() + "\n"


def apply_parsed_output(task: HarnessTask, parsed: ParsedHarnessOutput, raw_output: str) -> None:
    task.last_harness_output = raw_output
    if parsed.summary:
        task.current_summary = parsed.summary
    task.evidence.extend(parsed.evidence)
    task.actions.extend(parsed.actions)
    if parsed.can_stop:
        task.can_stop = parsed.can_stop
    existing_open = {q.get("id") for q in task.open_questions if q.get("status") == "awaiting_user"}
    for question in parsed.open_questions:
        if question.question_id in existing_open:
            continue
        task.open_questions.append(_question_to_dict(question))
    if parsed.open_questions:
        task.state = "awaiting_clarification"


def _question_to_dict(question: ParsedQuestion) -> dict:
    return {
        "id": question.question_id,
        "question": question.question,
        "choices": question.choices,
        "allow_freeform": question.allow_freeform,
        "why_needed": question.why_needed,
        "status": "awaiting_user",
        "answer": "",
    }


def latest_open_question(task: HarnessTask) -> dict | None:
    for question in reversed(task.open_questions):
        if question.get("status") == "awaiting_user":
            return question
    return None


def record_question_answer(task: HarnessTask, answer: str, *, question_id: str | None = None, actor: str = "") -> dict:
    target = None
    for question in reversed(task.open_questions):
        if question_id and question.get("id") != question_id:
            continue
        if question.get("status") == "awaiting_user":
            target = question
            break
    if target is None:
        raise KeyError("no pending harness clarification question")
    target["answer"] = answer
    target["status"] = "answered"
    target["answered_by"] = actor
    task.decision_log.append(
        DecisionLogEntry(
            time=__import__("plugins.harness_controller.controller", fromlist=["_now"])._now(),
            signal=f"Clarification answered by {actor or 'user'}.",
            evidence=f"Question: {target.get('question')} Answer: {answer}",
            decision="Resume harness with selected answer using durable handoff.",
            expected_outcome="Harness continues from the selected answer without repeating completed work.",
            action_sent="clarification_answer",
        )
    )
    task.state = "planning"
    return target


def build_revision_packet(task: HarnessTask, *, feedback: str = "") -> str:
    return f"""You are continuing a supervised harness task after user revision feedback.

Hermes task state is authoritative. Do not assume hidden prior context.
Continue in planning/prompt-construction mode. Do not execute the task, create cron, or launch auto mode until the user explicitly approves.

TASK
Task ID: {task.task_id}
Objective: {task.goal}
Harness: {task.harness}
Model: {task.model}
Approval action when final: {task.approval_action}

REVISION FEEDBACK
{feedback or '(none)'}

CURRENT PLAN / PROPOSAL
{task.plan_text or '(none)'}

LAST HARNESS OUTPUT
{(task.last_harness_output or '(none)')[-3000:]}

NEXT REQUIRED ACTION
Produce a revised plan/proposal. If more input is required, return a structured OPEN QUESTIONS section. Otherwise return the finalized proposal and CAN STOP.

Return structured sections: SUMMARY, EVIDENCE, DECISION, ACTIONS, VERIFICATION, OPEN QUESTIONS, CAN STOP.
"""


def build_handoff_packet(task: HarnessTask, *, user_answer: str = "") -> str:
    evidence = "\n".join(f"- {item}" for item in task.evidence[-12:]) or "- (none recorded)"
    actions = "\n".join(f"- {item}" for item in task.actions[-12:]) or "- (none recorded)"
    decisions = "\n".join(
        f"- {d.signal} Evidence: {d.evidence} Decision: {d.decision} Expected: {d.expected_outcome}"
        for d in task.decision_log[-8:]
    ) or "- (none recorded)"
    open_q = latest_open_question(task)
    question_text = open_q.get("question") if open_q else "(none pending)"
    return f"""You are continuing a supervised harness task.

Hermes task state is authoritative. Do not assume hidden prior context. Do not repeat completed work unless needed for verification. Treat the user's selected answer as binding unless unsafe.

TASK
Task ID: {task.task_id}
Objective: {task.goal}
Harness: {task.harness}
Model: {task.model}
State: {task.state}
Can stop: {task.can_stop}

USER ANSWER
Question: {question_text}
Answer: {user_answer or '(none)'}

CURRENT SUMMARY
{task.current_summary or '(none)'}

EVIDENCE
{evidence}

DECISIONS SO FAR
{decisions}

ACTIONS ALREADY TAKEN
{actions}

LAST HARNESS OUTPUT
{(task.last_harness_output or '(none)')[-3000:]}

NEXT REQUIRED ACTION
Continue from the user's answer in planning/prompt-construction mode. Follow inspect → decide → act → verify → log. If more user input is required, return an OPEN QUESTIONS section. Do not execute the task, create cron, or launch auto mode until the user explicitly approves. Do not duplicate expensive active jobs.

Return structured sections: SUMMARY, EVIDENCE, DECISION, ACTIONS, VERIFICATION, OPEN QUESTIONS, CAN STOP.
"""
