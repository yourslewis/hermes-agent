from __future__ import annotations


def test_supervisor_detects_repeated_waiting_and_sends_unblock_prompt():
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.supervisor import classify_harness_output, apply_supervisor_signal

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:1", "copilot", "gpt-5.4", "Fix AML")
    controller.attach_plan(task.task_id, plan_text="Plan", auto_prompt="Run")
    def _launch_auto(task_id: str) -> None:
        task = controller.get_task(task_id)
        task.state = "running_auto"

    _launch_auto(task.task_id)

    signal = classify_harness_output("Still waiting... still waiting for the job to move")
    prompt = apply_supervisor_signal(controller, task.task_id, signal)

    updated = controller.get_task(task.task_id)
    assert signal.kind == "stall"
    assert updated.state == "unblocking"
    assert prompt is not None
    assert "Do not keep waiting" in prompt
    assert updated.decision_log[-1].decision == "Send unblock/status-check instruction to harness."


def test_supervisor_escalates_dangerous_actions_in_auto_mode():
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.supervisor import classify_harness_output, apply_supervisor_signal

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:1", "codex", "gpt-5.6-sol", "Fix repo")
    controller.attach_plan(task.task_id, plan_text="Plan", auto_prompt="Run")
    def _launch_auto(task_id: str) -> None:
        task = controller.get_task(task_id)
        task.state = "running_auto"

    _launch_auto(task.task_id)

    signal = classify_harness_output("Can I force push to main to fix it?")
    prompt = apply_supervisor_signal(controller, task.task_id, signal)

    updated = controller.get_task(task.task_id)
    assert signal.kind == "danger"
    assert prompt is None
    assert updated.state == "awaiting_user_escalation"
    assert updated.decision_log[-1].decision == "Escalate to Slack instead of auto-approving."


def test_supervisor_requests_verification_for_success_without_evidence():
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.supervisor import classify_harness_output, apply_supervisor_signal

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:1", "claude-code", "opus", "Fix bug")
    controller.attach_plan(task.task_id, plan_text="Plan", auto_prompt="Run")
    def _launch_auto(task_id: str) -> None:
        task = controller.get_task(task_id)
        task.state = "running_auto"

    _launch_auto(task.task_id)

    signal = classify_harness_output("Success. The issue is fixed.")
    prompt = apply_supervisor_signal(controller, task.task_id, signal)

    assert signal.kind == "missing_evidence"
    assert prompt is not None
    assert "verification evidence" in prompt
    assert controller.get_task(task.task_id).decision_log[-1].decision == "Request verification evidence before completion."
