from __future__ import annotations

import asyncio


def test_copilot_auto_mapping_contains_full_autonomy_flags():
    from plugins.harness_controller.harnesses import build_harness_command

    spec = build_harness_command(
        harness="copilot",
        model="gpt-5.4",
        mode="auto",
        prompt="Fix the issue",
        workdir="/repo",
    )

    assert spec.argv[:2] == ["copilot", "-p"]
    assert "--model" in spec.argv
    assert "gpt-5.4" in spec.argv
    assert "--reasoning-effort" in spec.argv
    assert "xhigh" in spec.argv
    for flag in [
        "--allow-all",
        "--allow-all-tools",
        "--allow-all-paths",
        "--allow-all-urls",
        "--no-ask-user",
        "--autopilot",
    ]:
        assert flag in spec.argv
    assert "AUTO execution mode" in spec.prompt
    assert "If progress slows" in spec.prompt


def test_copilot_plan_mapping_avoids_autonomy_flags():
    from plugins.harness_controller.harnesses import build_harness_command

    spec = build_harness_command(
        harness="copilot",
        model="gpt-5.4",
        mode="plan",
        prompt="Fix the issue",
        workdir="/repo",
    )

    forbidden = {
        "--allow-all",
        "--allow-all-tools",
        "--allow-all-paths",
        "--allow-all-urls",
        "--autopilot",
    }
    assert forbidden.isdisjoint(spec.argv)
    assert "PLAN mode" in spec.prompt
    assert "Do not edit files" in spec.prompt
    assert "Expected success signals" in spec.prompt


def test_codex_mode_mappings_use_top_level_policy_flags():
    from plugins.harness_controller.harnesses import build_harness_command

    plan = build_harness_command(
        harness="codex",
        model="gpt-5.6-sol",
        mode="plan",
        prompt="Plan only",
        workdir="/repo",
    )
    auto = build_harness_command(
        harness="codex",
        model="gpt-5.6-sol",
        mode="auto",
        prompt="Execute",
        workdir="/repo",
    )

    assert plan.argv[:7] == ["codex", "--ask-for-approval", "on-request", "--sandbox", "read-only", "exec", "--skip-git-repo-check"]
    assert "--model" in plan.argv
    assert "gpt-5.6-sol" in plan.argv
    assert "--cd" in plan.argv
    assert "/repo" in plan.argv
    assert "PLAN mode" in plan.prompt

    assert auto.argv[:7] == ["codex", "--ask-for-approval", "never", "--sandbox", "danger-full-access", "exec", "--skip-git-repo-check"]
    assert "AUTO execution mode" in auto.prompt
    assert "Do not submit duplicate expensive full jobs" in auto.prompt


def test_opencode_mode_mappings_select_plan_vs_build_agent():
    from plugins.harness_controller.harnesses import build_harness_command

    plan = build_harness_command(
        harness="opencode",
        model="litellm/gpt-5.5",
        mode="plan",
        prompt="Plan only",
        workdir="/repo",
    )
    auto = build_harness_command(
        harness="opencode",
        model="litellm/gpt-5.5",
        mode="auto",
        prompt="Execute",
        workdir="/repo",
    )

    assert plan.argv[:2] == ["opencode", "run"]
    assert "--agent" in plan.argv
    assert plan.argv[plan.argv.index("--agent") + 1] == "plan"
    assert "--auto" not in plan.argv

    assert "--agent" in auto.argv
    assert auto.argv[auto.argv.index("--agent") + 1] == "build"
    assert "--auto" in auto.argv
    assert "AUTO execution mode" in auto.prompt


def test_claude_code_mode_mappings_use_permission_modes():
    from plugins.harness_controller.harnesses import build_harness_command

    plan = build_harness_command(
        harness="claude-code",
        model="claude-sonnet-4-6",
        mode="plan",
        prompt="Plan only",
        workdir="/repo",
    )
    auto = build_harness_command(
        harness="claude-code",
        model="claude-sonnet-4-6",
        mode="auto",
        prompt="Execute",
        workdir="/repo",
    )

    assert plan.argv[:2] == ["claude", "-p"]
    assert "--permission-mode" in plan.argv
    assert plan.argv[plan.argv.index("--permission-mode") + 1] == "plan"
    assert "--dangerously-skip-permissions" not in plan.argv

    assert "--permission-mode" in auto.argv
    assert auto.argv[auto.argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "--dangerously-skip-permissions" in auto.argv
    assert "AUTO execution mode" in auto.prompt


def test_task_state_approve_is_idempotent_and_logs_decision():
    from plugins.harness_controller.controller import HarnessController

    controller = HarnessController.in_memory()
    task = controller.create_task(
        thread_key="slack:C1:123.4",
        harness="copilot",
        model="gpt-5.4",
        goal="Fix the issue",
    )
    controller.attach_plan(task.task_id, plan_text="Plan", auto_prompt="Run it")

    first = controller.approve(task.task_id, actor="U1")
    second = controller.approve(task.task_id, actor="U1")

    assert first.changed is True
    assert second.changed is False
    assert controller.get_task(task.task_id).state == "approved"
    assert controller.get_task(task.task_id).decision_log[-1].decision == "Approved plan for terminal action launch_auto."


def test_cron_setup_uses_same_states_with_create_cron_action_and_custom_labels():
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.slack_blocks import build_plan_blocks

    controller = HarnessController.in_memory()
    task = controller.create_task(
        "slack:C1:123.4",
        "claude-code",
        "claude-opus-4-8",
        "Use long-task-supervision to supervise X",
        approval_action="create_cron",
        approve_label="Approve & create cron",
        revise_label="Revise instruction",
    )
    controller.attach_plan(task.task_id, plan_text="Cron proposal", auto_prompt="cron prompt")

    blocks = build_plan_blocks(task)
    elements = [b for b in blocks if b.get("type") == "actions"][0]["elements"]

    assert task.state == "plan_ready"
    assert task.approval_action == "create_cron"
    assert elements[0]["text"]["text"] == "Approve & create cron"
    assert elements[0]["action_id"] == "harness_approve"
    assert elements[1]["text"]["text"] == "Revise instruction"


def test_answering_clarification_continues_planning_not_auto():
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.parser import parse_harness_output
    from plugins.harness_controller.store import apply_parsed_output, record_question_answer

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:100", "codex", "gpt-5.6-sol", "Prepare cron setup", approval_action="create_cron")
    apply_parsed_output(task, parse_harness_output("""
## OPEN QUESTIONS
- id: q001
  question: Which verification?
  choices:
    - id: file_exists
      label: File exists
  allow_freeform: true
"""), "raw")

    record_question_answer(task, "file_exists: File exists", actor="U1")

    assert task.state == "planning"
    assert task.open_questions[-1]["status"] == "answered"


def test_revise_moves_plan_ready_task_back_to_revising_then_planning_prompt():
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.store import build_revision_packet

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:100", "opencode", "litellm/gpt-5.5", "Draft prompt", approval_action="accept_prompt")
    controller.attach_plan(task.task_id, plan_text="Initial prompt", auto_prompt="Initial prompt")

    result = controller.request_revision(task.task_id, feedback="Ask one more verification question", actor="U1")
    packet = build_revision_packet(task, feedback="Ask one more verification question")

    assert result.changed is True
    assert task.state == "revising"
    assert "Ask one more verification question" in packet
    assert "Continue in planning/prompt-construction mode" in packet


def test_slack_plan_blocks_include_action_tokens():
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.slack_blocks import build_plan_blocks

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:123.4", "copilot", "gpt-5.4", "Fix it")
    controller.attach_plan(task.task_id, plan_text="1. Inspect\n2. Fix", auto_prompt="Fix it")

    blocks = build_plan_blocks(controller.get_task(task.task_id))
    actions = [block for block in blocks if block.get("type") == "actions"]
    assert actions
    elements = actions[0]["elements"]
    assert {e["action_id"] for e in elements} == {
        "harness_approve",
        "harness_revise_plan",
        "harness_cancel",
    }
    for element in elements:
        assert element["value"] == task.task_id


def test_slack_plan_blocks_keep_each_section_under_slack_limit_for_long_codex_output():
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.slack_blocks import build_plan_blocks

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:123.4", "codex", "gpt-5.6-sol", "Fix it " + "very-long-scope " * 180)
    controller.attach_plan(task.task_id, plan_text="Codex plan line.\n" * 500, auto_prompt="Run it")
    task.canvas_url = "https://canvas.wenhao.dev/harness?run=hrun_test&tab=codex"
    task.remote_cli_url = "https://canvas.wenhao.dev/ui/codex-cli/"
    task.vscode_url = "https://canvas.wenhao.dev/ui/vscode/?folder=%2Fvery%2Flong%2Frepo"

    blocks = build_plan_blocks(task)
    section_texts = [block["text"]["text"] for block in blocks if block.get("type") == "section"]

    assert len(section_texts) > 1
    assert all(len(text) <= 3000 for text in section_texts)
    assert any("Codex plan line." in text for text in section_texts)
    assert blocks[-1]["type"] == "actions"


def test_slack_approve_callback_acks_and_launches_auto():
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.slack_actions import handle_approve_auto

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:123.4", "copilot", "gpt-5.4", "Fix it")
    controller.attach_plan(task.task_id, plan_text="Plan", auto_prompt="Run it")
    launched = []

    async def fake_launch(task_id: str):
        launched.append(task_id)

    acked = []

    async def ack():
        acked.append(True)

    asyncio.run(
        handle_approve_auto(
            ack=ack,
            body={"user": {"id": "U1"}, "response_url": "https://example.invalid/response"},
            action={"value": task.task_id},
            controller=controller,
            launch_auto=fake_launch,
            post_response=lambda *_args, **_kwargs: None,
        )
    )

    assert acked == [True]
    assert launched == [task.task_id]
    assert controller.get_task(task.task_id).state == "approved"


def test_slack_ack_payloads_update_message_and_post_thread_reply():
    from plugins.harness_controller.slack_actions import build_slack_ack_payloads

    body = {
        "channel": {"id": "C1"},
        "message": {"ts": "111.222", "thread_ts": "100.000"},
        "container": {"thread_ts": "100.000"},
    }

    update_payload, post_payload = build_slack_ack_payloads(body, "✅ Approved")

    assert update_payload == {
        "channel": "C1",
        "ts": "111.222",
        "text": "✅ Approved",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "✅ Approved"}}],
    }
    assert post_payload == {
        "channel": "C1",
        "text": "✅ Approved",
        "thread_ts": "100.000",
    }


def test_slack_thread_message_payload_reuses_plan_thread():
    from plugins.harness_controller.slack_actions import build_slack_message_payload

    body = {
        "channel": {"id": "C1"},
        "message": {"ts": "111.222", "thread_ts": "100.000"},
    }

    payload = build_slack_message_payload(body, "🚀 Auto execution started")

    assert payload == {
        "channel": "C1",
        "text": "🚀 Auto execution started",
        "thread_ts": "100.000",
    }


def test_parse_structured_open_question_and_can_stop():
    from plugins.harness_controller.parser import parse_harness_output

    parsed = parse_harness_output(
        """
## SUMMARY
Need user choice.

## OPEN QUESTIONS
- id: q001
  question: Which verification should I run?
  choices:
    - id: smoke
      label: Smoke test
      consequence: Cheap and safe
    - id: integration
      label: Integration test
      consequence: Slower but thorough
  allow_freeform: true
  why_needed: Verification path is ambiguous.

## CAN STOP
no — verification not complete
"""
    )

    assert parsed.summary == "Need user choice."
    assert parsed.can_stop == "no — verification not complete"
    assert len(parsed.open_questions) == 1
    q = parsed.open_questions[0]
    assert q.question_id == "q001"
    assert q.question == "Which verification should I run?"
    assert q.allow_freeform is True
    assert q.choices[0]["id"] == "smoke"
    assert q.choices[0]["label"] == "Smoke test"


def test_parse_open_questions_none_pending_does_not_create_question():
    from plugins.harness_controller.parser import parse_harness_output

    parsed = parse_harness_output(
        """
## SUMMARY
Decision resolved.

## OPEN QUESTIONS
None pending. The decision is resolved: smoke test runs first.

## CAN STOP
Yes — clarification complete.
"""
    )

    assert parsed.open_questions == []
    assert parsed.can_stop == "Yes — clarification complete."


def test_task_store_roundtrip_and_handoff_packet(tmp_path):
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.parser import parse_harness_output
    from plugins.harness_controller.store import HarnessTaskStore, apply_parsed_output, build_handoff_packet, record_question_answer

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:100", "claude-code", "claude-opus-4-8", "Verify AML")
    apply_parsed_output(
        task,
        parse_harness_output(
            """
## SUMMARY
Need verification choice.
## EVIDENCE
- e001: parent run is active
## ACTIONS
- a001: inspected run status
## OPEN QUESTIONS
- id: q001
  question: Which probe?
  choices:
    - id: smoke
      label: Smoke
  allow_freeform: true
## CAN STOP
no — root cause unknown
"""
        ),
        "raw output",
    )
    store = HarnessTaskStore(tmp_path)
    store.save(task)
    loaded = store.load(task.task_id)
    assert loaded.task_id == task.task_id
    assert loaded.evidence == ["- e001: parent run is active"]
    record_question_answer(loaded, "smoke: Smoke", actor="U1")
    packet = build_handoff_packet(loaded, user_answer="smoke: Smoke")
    assert "Task ID:" in packet
    assert "Verify AML" in packet
    assert "smoke: Smoke" in packet
    assert "Do not repeat completed work" in packet


def test_clarification_blocks_include_choice_and_freeform_buttons():
    from plugins.harness_controller.clarification_blocks import build_clarification_blocks
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.parser import parse_harness_output
    from plugins.harness_controller.store import apply_parsed_output

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:100", "codex", "gpt-5.6-sol", "Verify")
    apply_parsed_output(task, parse_harness_output("""
## OPEN QUESTIONS
- id: q001
  question: Which verification?
  choices:
    - id: smoke
      label: Smoke
  allow_freeform: true
"""), "raw")
    blocks = build_clarification_blocks(task)
    actions = [b for b in blocks if b.get("type") == "actions"][0]
    action_ids = {e["action_id"] for e in actions["elements"]}
    assert "harness_answer_choice_0" in action_ids
    assert "harness_answer_other" in action_ids
