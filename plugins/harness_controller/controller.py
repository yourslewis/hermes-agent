from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

TaskState = Literal[
    "planning",
    "awaiting_clarification",
    "plan_ready",
    "revising",
    "approved",
    "running_or_creating",
    "done",
    "cancelled",
    "failed",
    # Legacy/supervisor compatibility states kept for existing tests and stored tasks.
    "created",
    "awaiting_approval",
    "running_auto",
    "unblocking",
    "awaiting_user_escalation",
]

ApprovalAction = Literal["launch_auto", "create_cron", "accept_prompt"]


@dataclass
class DecisionLogEntry:
    time: str
    signal: str
    evidence: str
    decision: str
    expected_outcome: str
    action_sent: str = ""


@dataclass
class HarnessTask:
    task_id: str
    thread_key: str
    harness: str
    model: str
    goal: str
    state: TaskState = "planning"
    approval_action: ApprovalAction = "launch_auto"
    approve_label: str = "Approve & run auto"
    revise_label: str = "Revise plan"
    cancel_label: str = "Cancel"
    plan_text: str = ""
    auto_prompt: str = ""
    last_harness_output: str = ""
    current_summary: str = ""
    evidence: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    open_questions: list[dict] = field(default_factory=list)
    can_stop: str = "no — verification not complete"
    plan_revision: int = 0
    run_id: str = ""
    canvas_url: str = ""
    remote_cli_url: str = ""
    vscode_url: str = ""
    decision_log: list[DecisionLogEntry] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: _now())
    updated_at: str = field(default_factory=lambda: _now())


@dataclass(frozen=True)
class TransitionResult:
    changed: bool
    task: HarnessTask
    message: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_task_id() -> str:
    return "htask_" + uuid.uuid4().hex[:12]


def _default_approve_label(action: ApprovalAction) -> str:
    if action == "create_cron":
        return "Approve & create cron"
    if action == "accept_prompt":
        return "Accept prompt"
    return "Approve & run auto"


def _default_revise_label(action: ApprovalAction) -> str:
    if action == "create_cron":
        return "Revise instruction"
    if action == "accept_prompt":
        return "Revise prompt"
    return "Revise plan"


def _expected_outcome_for(action: ApprovalAction) -> str:
    if action == "create_cron":
        return "Hermes creates the approved cron job from the finalized instruction."
    if action == "accept_prompt":
        return "Prompt is accepted and no execution is launched."
    return "Harness executes approved plan and reports verification evidence."


class HarnessController:

    def __init__(self) -> None:
        self._tasks: dict[str, HarnessTask] = {}

    @classmethod
    def in_memory(cls) -> "HarnessController":
        return cls()

    def create_task(
        self,
        thread_key: str,
        harness: str,
        model: str,
        goal: str,
        *,
        approval_action: ApprovalAction = "launch_auto",
        approve_label: str | None = None,
        revise_label: str | None = None,
        cancel_label: str = "Cancel",
    ) -> HarnessTask:
        task = HarnessTask(
            task_id=_new_task_id(),
            thread_key=thread_key,
            harness=harness,
            model=model,
            goal=goal,
            state="planning",
            approval_action=approval_action,
            approve_label=approve_label or _default_approve_label(approval_action),
            revise_label=revise_label or _default_revise_label(approval_action),
            cancel_label=cancel_label,
        )
        self._tasks[task.task_id] = task
        return task

    def get_task(self, task_id: str) -> HarnessTask:
        try:
            return self._tasks[task_id]
        except KeyError as exc:
            raise KeyError(f"unknown harness task: {task_id}") from exc

    def remember_task(self, task: HarnessTask) -> HarnessTask:
        self._tasks[task.task_id] = task
        return task

    def attach_plan(self, task_id: str, *, plan_text: str, auto_prompt: str) -> HarnessTask:
        task = self.get_task(task_id)
        task.plan_text = plan_text
        task.auto_prompt = auto_prompt
        task.plan_revision += 1
        task.state = "plan_ready"
        task.updated_at = _now()
        return task

    def approve(self, task_id: str, *, actor: str = "") -> TransitionResult:
        task = self.get_task(task_id)
        if task.state in {"approved", "running_or_creating", "running_auto"}:
            return TransitionResult(False, task, "Task is already approved or running.")
        if task.state in {"cancelled", "done", "failed"}:
            return TransitionResult(False, task, f"Task is already {task.state}.")
        task.state = "approved"
        task.updated_at = _now()
        task.decision_log.append(
            DecisionLogEntry(
                time=_now(),
                signal=f"Plan approved by {actor or 'user'}.",
                evidence=f"Plan revision {task.plan_revision} approved from Slack action.",
                decision=f"Approved plan for terminal action {task.approval_action}.",
                expected_outcome=_expected_outcome_for(task.approval_action),
                action_sent=task.approval_action,
            )
        )
        return TransitionResult(True, task, f"Approved: {task.approval_action}.")

    def approve_for_auto(self, task_id: str, *, actor: str = "") -> TransitionResult:
        """Backward-compatible wrapper for older tests/callbacks."""
        result = self.approve(task_id, actor=actor)
        if result.changed:
            result.task.state = "running_auto"
            result.task.decision_log[-1].decision = "Approved plan and started auto execution."
            result.task.decision_log[-1].expected_outcome = "Harness executes approved plan and reports verification evidence."
            result.task.decision_log[-1].action_sent = "auto_launch"
        return result

    def request_revision(self, task_id: str, *, feedback: str, actor: str = "") -> TransitionResult:
        task = self.get_task(task_id)
        if task.state in {"cancelled", "done", "failed"}:
            return TransitionResult(False, task, f"Task is already {task.state}.")
        task.state = "revising"
        task.updated_at = _now()
        task.decision_log.append(
            DecisionLogEntry(
                time=_now(),
                signal=f"Revision requested by {actor or 'user'}.",
                evidence=feedback,
                decision="Continue planning with revision feedback.",
                expected_outcome="Harness produces a revised plan/proposal without executing the task.",
                action_sent="revision_requested",
            )
        )
        return TransitionResult(True, task, "Revision requested.")

    def cancel(self, task_id: str, *, actor: str = "") -> TransitionResult:
        task = self.get_task(task_id)
        if task.state == "cancelled":
            return TransitionResult(False, task, "Task is already cancelled.")
        if task.state in {"done", "failed"}:
            return TransitionResult(False, task, f"Task is already {task.state}.")
        task.state = "cancelled"
        task.updated_at = _now()
        task.decision_log.append(
            DecisionLogEntry(
                time=_now(),
                signal=f"Cancelled by {actor or 'user'}.",
                evidence="Slack cancel action received.",
                decision="Cancelled task before auto execution.",
                expected_outcome="No harness auto process starts.",
                action_sent="cancel",
            )
        )
        return TransitionResult(True, task, "Task cancelled.")
