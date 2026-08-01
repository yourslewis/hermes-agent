from __future__ import annotations


def test_harness_run_store_creates_canvas_url_and_resume_commands(tmp_path):
    from plugins.harness_controller.run_registry import HarnessRunStore

    store = HarnessRunStore(tmp_path)
    run = store.create_run(
        task_id="htask_abc",
        thread_key="slack:C1:123.4",
        harness="claude-code",
        model="sonnet",
        mode="auto",
        goal="Fix the bug",
        workdir="/repo",
        source={"platform": "slack", "channel_id": "C1", "thread_ts": "123.4"},
    )

    assert run.run_id.startswith("hrun_")
    assert run.links["canvas"] == f"https://canvas.wenhao.dev/harness?run={run.run_id}&tab=claude-code"
    assert run.commands["resume"] == "cd /repo && claude --resume <session_id>"
    assert (tmp_path / "runs" / run.run_id / "run.json").exists()

    loaded = store.load(run.run_id)
    assert loaded.run_id == run.run_id
    assert loaded.harness == "claude-code"
    assert loaded.native["transcript_path"].endswith("transcript.jsonl")


def test_session_id_capture_updates_resume_command_and_transcript(tmp_path):
    from plugins.harness_controller.run_registry import HarnessRunStore, detect_native_session_id

    store = HarnessRunStore(tmp_path)
    run = store.create_run(
        task_id="htask_abc",
        thread_key="slack:C1:123.4",
        harness="codex",
        model="gpt-5.6-sol",
        mode="auto",
        goal="Fix the bug",
        workdir="/repo",
        source={},
    )
    output = '{"session_id":"13813116-3ae8-4fe5-a3cf-7e649647cba4","type":"result"}\nDone'

    assert detect_native_session_id("codex", output) == "13813116-3ae8-4fe5-a3cf-7e649647cba4"
    updated = store.record_result(run.run_id, exit_code=0, output=output)

    assert updated.native["session_id"] == "13813116-3ae8-4fe5-a3cf-7e649647cba4"
    assert updated.commands["resume"] == "cd /repo && codex resume 13813116-3ae8-4fe5-a3cf-7e649647cba4"
    transcript = tmp_path / "runs" / run.run_id / "transcript.jsonl"
    assert "session_id" in transcript.read_text(encoding="utf-8")


def test_slack_plan_blocks_include_canvas_link(tmp_path):
    from plugins.harness_controller.controller import HarnessController
    from plugins.harness_controller.run_registry import HarnessRunStore
    from plugins.harness_controller.slack_blocks import build_plan_blocks

    controller = HarnessController.in_memory()
    task = controller.create_task("slack:C1:123.4", "opencode", "litellm/gpt-5.5", "Fix it")
    controller.attach_plan(task.task_id, plan_text="Plan", auto_prompt="Run")
    run = HarnessRunStore(tmp_path).create_run(
        task_id=task.task_id,
        thread_key=task.thread_key,
        harness=task.harness,
        model=task.model,
        mode="plan",
        goal=task.goal,
        workdir="/repo",
        source={},
    )
    task.run_id = run.run_id
    task.canvas_url = run.links["canvas"]

    text = build_plan_blocks(task)[0]["text"]["text"]

    assert "Open in Canvas" in text
    assert "https://canvas.wenhao.dev/harness?run=" in text
