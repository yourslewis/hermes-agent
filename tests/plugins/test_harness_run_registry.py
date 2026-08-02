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
    assert run.links["remote_cli"] == "https://canvas.wenhao.dev/ui/claude-code-cli/"
    assert run.links["vscode"].startswith("https://canvas.wenhao.dev/ui/vscode/")
    assert "folder=%2Frepo" in run.links["vscode"]
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
    task.remote_cli_url = run.links["remote_cli"]
    task.vscode_url = run.links["vscode"]

    text = build_plan_blocks(task)[0]["text"]["text"]

    assert "Open in Canvas" in text
    assert "Remote CLI" in text
    assert "VS Code" in text
    assert "https://canvas.wenhao.dev/harness?run=" in text
    assert "https://canvas.wenhao.dev/ui/opencode-cli/" in text
    assert "https://canvas.wenhao.dev/ui/vscode/" in text



def test_list_runs_groups_threads_by_agent_default_first_then_latest(tmp_path):
    from datetime import datetime, timezone
    from plugins.harness_controller.preferences import HarnessPreference, HarnessPreferenceStore
    from plugins.harness_controller.run_registry import HarnessRunStore

    pref_store = HarnessPreferenceStore(tmp_path / "cfg")
    pref_store.set("default", HarnessPreference(harness="codex", model="gpt-5.6-sol", mode="plan"))
    store = HarnessRunStore(tmp_path)
    old = store.create_run(
        task_id="htask_old",
        thread_key="slack:C1:111.1",
        harness="opencode",
        model="litellm/gpt-5.5",
        mode="plan",
        goal="Older OpenCode task",
        source={"agent": "Selin", "thread_ts": "111.1"},
    )
    new = store.create_run(
        task_id="htask_new",
        thread_key="slack:C1:222.2",
        harness="claude-code",
        model="sonnet",
        mode="plan",
        goal="Newer Claude task with a long description that should still be summarized simply",
        source={"agent": "Selin", "thread_ts": "222.2"},
    )
    other = store.create_run(
        task_id="htask_other",
        thread_key="slack:C1:333.3",
        harness="codex",
        model="gpt-5.6-sol",
        mode="plan",
        goal="Rex task",
        source={"agent": "Rex", "thread_ts": "333.3"},
    )

    # Force deterministic recency ordering independent of filesystem timing.
    for run, ts in [
        (old, "2026-08-01T10:00:00+00:00"),
        (new, "2026-08-02T10:00:00+00:00"),
        (other, "2026-08-02T09:00:00+00:00"),
    ]:
        run_path = store.run_dir(run.run_id) / "run.json"
        data = __import__("json").loads(run_path.read_text(encoding="utf-8"))
        data["updated_at"] = ts
        run_path.write_text(__import__("json").dumps(data), encoding="utf-8")

    grouped = store.list_grouped_by_agent(pref_store)

    selin = next(agent for agent in grouped["agents"] if agent["agent"] == "Selin")
    assert selin["default_harness"]["harness"] == "codex"
    assert [thread["run_id"] for thread in selin["threads"]] == [new.run_id, old.run_id]
    assert selin["threads"][0]["latest_message_time"] == "2026-08-02T10:00:00+00:00"
    assert selin["threads"][0]["about"] == "Newer Claude task with a long description that should still be summarized simply"
    assert {agent["agent"] for agent in grouped["agents"]} == {"Selin", "Rex"}


def test_list_runs_uses_record_result_time_as_latest_message_time(tmp_path):
    from plugins.harness_controller.preferences import HarnessPreferenceStore
    from plugins.harness_controller.run_registry import HarnessRunStore

    store = HarnessRunStore(tmp_path)
    run = store.create_run(
        task_id="htask_done",
        thread_key="slack:C1:444.4",
        harness="codex",
        model="gpt-5.6-sol",
        mode="auto",
        goal="Verify dashboard sorting",
        source={"agent": "Don", "thread_ts": "444.4"},
    )
    store.record_result(run.run_id, exit_code=0, output="Done")

    grouped = store.list_grouped_by_agent(HarnessPreferenceStore(tmp_path / "cfg"))
    don = next(agent for agent in grouped["agents"] if agent["agent"] == "Don")

    assert don["threads"][0]["latest_message_time"] == don["threads"][0]["updated_at"]
    assert don["threads"][0]["about"] == "Verify dashboard sorting"



def test_agent_specific_default_harness_overrides_global_default(tmp_path):
    from plugins.harness_controller.preferences import HarnessPreference, HarnessPreferenceStore
    from plugins.harness_controller.run_registry import HarnessRunStore

    pref_store = HarnessPreferenceStore(tmp_path / "cfg")
    pref_store.set("default", HarnessPreference(harness="codex", model="gpt-5.6-sol", mode="plan"))
    pref_store.set("selin", HarnessPreference(harness="opencode", model="litellm/gpt-5.5", mode="auto"))
    store = HarnessRunStore(tmp_path)
    store.create_run(
        task_id="htask_selin",
        thread_key="slack:C1:555.5",
        harness="claude-code",
        model="sonnet",
        mode="plan",
        goal="Selin task",
        source={"agent": "Selin"},
    )

    grouped = store.list_grouped_by_agent(pref_store)
    selin = next(agent for agent in grouped["agents"] if agent["agent"] == "Selin")

    assert selin["default_harness"]["harness"] == "opencode"
    assert selin["default_harness"]["model"] == "litellm/gpt-5.5"
