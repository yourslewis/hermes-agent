from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .controller import DecisionLogEntry, HarnessController, _now

SignalKind = Literal["progress", "stall", "block", "danger", "missing_evidence", "unknown"]


@dataclass(frozen=True)
class SupervisorSignal:
    kind: SignalKind
    signal: str
    evidence: str


UNBLOCK_PROMPT = """Status check required. Do not keep waiting.
Inspect live state, logs, status, resources, and scheduler/queue state now.
Report evidence, choose the safest reversible next action, and continue.
If this is an expensive duplicate/full run, stop and use a cheap canary or monitor the existing run instead.
"""


def classify_harness_output(text: str, *, seconds_since_output: int = 0) -> SupervisorSignal:
    lower = text.lower()
    if seconds_since_output >= 900:
        return SupervisorSignal("stall", f"No output for {seconds_since_output}s.", "Harness process is alive but has not emitted output.")
    if lower.count("waiting") >= 2 or "still waiting" in lower or "queued" in lower:
        return SupervisorSignal("stall", "Harness appears to be waiting instead of diagnosing.", text[-500:])
    if any(term in lower for term in ["force push", "delete production", "drop database", "rm -rf /", "spend more"]):
        return SupervisorSignal("danger", "Harness proposed a destructive/public/cost-expanding action.", text[-500:])
    if any(term in lower for term in ["may i", "can i", "permission", "approve"]):
        return SupervisorSignal("block", "Harness requested permission or input.", text[-500:])
    if "success" in lower and not any(term in lower for term in ["test", "verified", "passed", "evidence", "output"]):
        return SupervisorSignal("missing_evidence", "Harness claimed success without verification evidence.", text[-500:])
    if any(term in lower for term in ["edited", "running", "passed", "failed", "log", "status", "inspected"]):
        return SupervisorSignal("progress", "Harness reported observable progress.", text[-500:])
    return SupervisorSignal("unknown", "No actionable supervisor signal detected.", text[-500:])


def apply_supervisor_signal(controller: HarnessController, task_id: str, signal: SupervisorSignal) -> str | None:
    task = controller.get_task(task_id)
    if signal.kind == "stall":
        task.state = "unblocking"
        task.decision_log.append(
            DecisionLogEntry(
                time=_now(),
                signal=signal.signal,
                evidence=signal.evidence,
                decision="Send unblock/status-check instruction to harness.",
                expected_outcome="Harness inspects live state and takes the safest reversible next action.",
                action_sent=UNBLOCK_PROMPT,
            )
        )
        return UNBLOCK_PROMPT
    if signal.kind == "danger":
        task.state = "awaiting_user_escalation"
        task.decision_log.append(
            DecisionLogEntry(
                time=_now(),
                signal=signal.signal,
                evidence=signal.evidence,
                decision="Escalate to Slack instead of auto-approving.",
                expected_outcome="User explicitly approves, revises, or cancels the risky action.",
                action_sent="slack_escalation",
            )
        )
        return None
    if signal.kind == "missing_evidence":
        prompt = "Do not declare success yet. Run or report concrete verification evidence, including exact command output or live system status."
        task.decision_log.append(
            DecisionLogEntry(
                time=_now(),
                signal=signal.signal,
                evidence=signal.evidence,
                decision="Request verification evidence before completion.",
                expected_outcome="Harness produces real evidence or marks the task blocked.",
                action_sent=prompt,
            )
        )
        return prompt
    return None
