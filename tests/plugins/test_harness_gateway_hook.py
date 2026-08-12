from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_hrun_hook_skips_gateway_and_posts_plan_blocks(monkeypatch):
    import plugins.harness_controller as hc

    posts = []

    class Client:
        async def chat_postMessage(self, **kwargs):
            posts.append(kwargs)
            return {"ts": "1.2"}

    class Adapter:
        def _get_client(self, chat_id):
            return Client()

        async def send(self, chat_id, text, metadata=None):
            posts.append({"channel": chat_id, "text": text, "metadata": metadata})

    class Platform:
        value = "slack"

    source = SimpleNamespace(platform=Platform(), chat_id="C1", thread_id="123.4")
    event = SimpleNamespace(text="/hrun --harness copilot --model gpt-5.4 Test task", source=source, message_id="123.4")
    gateway = SimpleNamespace(adapters={source.platform: Adapter()})

    async def fake_run_capture(argv, cwd=None, timeout=600):
        return 0, "Plan body"

    monkeypatch.setattr(hc, "_run_capture", fake_run_capture)
    result = hc._pre_gateway_dispatch(event=event, gateway=gateway)

    assert result == {"action": "skip", "reason": "harness controller handling /hrun"}
    assert any("Planning `Test task`" in post["text"] for post in posts)
    plan_posts = [post for post in posts if "blocks" in post]
    assert plan_posts
    action_blocks = [block for block in plan_posts[-1]["blocks"] if block.get("type") == "actions"]
    assert action_blocks
    assert action_blocks[-1]["elements"][0]["action_id"] == "harness_approve"



def test_hrun_hook_posts_long_codex_plan_without_oversized_slack_sections(monkeypatch):
    import plugins.harness_controller as hc

    posts = []

    class Client:
        async def chat_postMessage(self, **kwargs):
            for block in kwargs.get("blocks") or []:
                if block.get("type") == "section":
                    assert len(block["text"]["text"]) <= 3000
            posts.append(kwargs)
            return {"ts": "1.2"}

    class Adapter:
        def _get_client(self, chat_id):
            return Client()

        async def send(self, chat_id, text, metadata=None):
            posts.append({"channel": chat_id, "text": text, "metadata": metadata})

    class Platform:
        value = "slack"

    source = SimpleNamespace(platform=Platform(), chat_id="C1", thread_id="123.4")
    event = SimpleNamespace(text="/hrun --harness codex --model gpt-5.6-sol " + "Fix it " * 300, source=source, message_id="123.4")
    gateway = SimpleNamespace(adapters={source.platform: Adapter()})

    async def fake_run_capture(argv, cwd=None, timeout=600):
        return 0, "Codex plan body.\n" * 900

    monkeypatch.setattr(hc, "_run_capture", fake_run_capture)

    result = hc._pre_gateway_dispatch(event=event, gateway=gateway)

    assert result == {"action": "skip", "reason": "harness controller handling /hrun"}
    assert any("blocks" in post for post in posts)


def test_source_dict_records_current_agent_from_environment(monkeypatch):
    import plugins.harness_controller as hc

    class Platform:
        value = "slack"

    source = SimpleNamespace(platform=Platform(), chat_id="C1", thread_id="123.4")
    event = SimpleNamespace(source=source, message_id="123.4")
    monkeypatch.setenv("HERMES_PROFILE", "selin")

    data = hc._source_dict_from_event(event)

    assert data["agent"] == "selin"


def test_extract_presentable_reply_strips_exec_logs_and_canvas_footer():
    import plugins.harness_controller as hc

    raw = """
Reading additional input from stdin...
OpenAI Codex v0.142.0
--------
exec
/bin/zsh -lc 'ls -la'
 succeeded in 9ms:
total 1
codex
1. Objective
Understand repo layout.

2. Proposed actions
- Read README
- Inspect tests
tokens used
12,345
1. Objective
Understand repo layout.

2. Proposed actions
- Read README
- Inspect tests
[Canvas] Resume native opened in dedicated terminal: codex resume abc-123
"""

    cleaned = hc._extract_presentable_reply(raw, "codex")

    assert "1. Objective" in cleaned
    assert "2. Proposed actions" in cleaned
    assert "exec" not in cleaned
    assert "tokens used" not in cleaned
    assert "[Canvas]" not in cleaned


def test_extract_presentable_reply_keeps_only_copilot_final_answer():
    import plugins.harness_controller as hc

    raw = """
I'll start by inspecting the environment.

● Inspect working dir and tooling (shell)
  │ pwd; ls -la; git --version
  └ 38 lines…

Tooling works. Attempting the clone.

● Clone repository (shell) 3m 0s
  │ git clone https://example.test/repo
  └ 4 lines…

Clone succeeded. Surveying the repo.

● Verify generated documentation (shell)
  │ git status --short; ./check-links
  └ 22 lines…

Done. All verification passed.

## Summary

- 50/50 scripts documented.
- No source files modified.

Changes    +0 -0
Duration   12m 46s
Tokens     ↑ 1.2m • ↓ 30.5k
Resume     copilot --resume=abc-123
"""

    cleaned = hc._extract_presentable_reply(raw, "copilot")

    assert cleaned.startswith("Done. All verification passed.")
    assert "50/50 scripts documented" in cleaned
    assert "Inspect working dir" not in cleaned
    assert "git clone" not in cleaned
    assert "Tooling works" not in cleaned
    assert "Changes    +0 -0" not in cleaned
    assert "Resume     copilot" not in cleaned


def test_extract_presentable_reply_handles_copilot_denied_tool_before_plan():
    import plugins.harness_controller as hc

    raw = """
✗ Inspect working dir and tooling (shell)
  │ pwd; ls -la
  └ Permission denied and could not request permission from user

I couldn't inspect the environment, so this plan is based on your description.

## 1. Objective

Understand the repository.

Changes    +0 -0
Duration   51s
Tokens     ↑ 29.9k • ↓ 3.1k
Resume     copilot --resume=def-456
"""

    cleaned = hc._extract_presentable_reply(raw, "copilot")

    assert cleaned.startswith("I couldn't inspect the environment")
    assert "## 1. Objective" in cleaned
    assert "Permission denied" not in cleaned
    assert "Tokens" not in cleaned


def test_plan_mode_summary_request_temporarily_executes_and_returns_summary(monkeypatch):
    import plugins.harness_controller as hc

    posts = []

    class Client:
        async def chat_postMessage(self, **kwargs):
            posts.append(kwargs)
            return {"ts": "1.2"}

    class Adapter:
        def _get_client(self, chat_id):
            return Client()

        async def send(self, chat_id, text, metadata=None):
            posts.append({"channel": chat_id, "text": text, "metadata": metadata})

    class Platform:
        value = "slack"

    source = SimpleNamespace(platform=Platform(), chat_id="C1", thread_id="123.4")
    event = SimpleNamespace(
        text="/hrun --harness codex --model gpt-5.6-sol summarize this repo status",
        source=source,
        message_id="123.4",
    )
    gateway = SimpleNamespace(adapters={source.platform: Adapter()})

    async def fake_run_capture(argv, cwd=None, timeout=600):
        return 0, "codex\nRepository summary: all checks are green."

    monkeypatch.setattr(hc, "_run_capture", fake_run_capture)

    result = hc._pre_gateway_dispatch(event=event, gateway=gateway)

    assert result == {"action": "skip", "reason": "harness controller handling /hrun"}
    assert any("Executing `summarize this repo status`" in post["text"] for post in posts)
    assert any("temporary override for summary request" in post["text"] for post in posts)
    assert any("Default mode remains plan." in post["text"] for post in posts)
    assert not any("blocks" in post for post in posts)


def test_hsummary_executes_synthesis_for_canvas_run_in_temporary_ask_mode(monkeypatch):
    import plugins.harness_controller as hc

    source_run = SimpleNamespace(
        run_id="hrun_source123",
        task_id="htask_1",
        thread_key="slack:C1:123.4",
        harness="codex",
        model="gpt-5.6-sol",
        mode="auto",
        state="completed",
        workdir="",
        repo="",
        branch="",
        native={"transcript_path": ""},
        links={"canvas": "https://canvas.example/harness?run=hrun_source123"},
    )
    summary_run = SimpleNamespace(
        run_id="hrun_summary456",
        links={"canvas": "https://canvas.example/harness?run=hrun_summary456"},
    )
    captured = {"posts": [], "argv": []}

    monkeypatch.setattr(
        hc, "_find_latest_thread_run", lambda thread_key, selector="", exclude_ask=False: source_run
    )
    monkeypatch.setattr(hc, "_read_run_output", lambda run, task=None: "codex\nImplemented the feature and all tests passed.")
    monkeypatch.setattr(hc._controller, "get_task", lambda task_id: (_ for _ in ()).throw(KeyError(task_id)))
    monkeypatch.setattr(hc._store, "load", lambda task_id: (_ for _ in ()).throw(KeyError(task_id)))
    monkeypatch.setattr(hc._run_store, "create_run", lambda **kwargs: summary_run)
    monkeypatch.setattr(hc._run_store, "record_result", lambda *args, **kwargs: summary_run)

    async def fake_run_capture(argv, cwd=None, timeout=600):
        captured["argv"] = argv
        return 0, "codex\nCompleted the feature. Verification: all tests passed. No follow-ups."

    async def fake_post(gateway, event, text, blocks=None):
        captured["posts"].append(text)

    monkeypatch.setattr(hc, "_run_capture", fake_run_capture)
    monkeypatch.setattr(hc, "_post_to_thread", fake_post)

    class Platform:
        value = "slack"

    event = SimpleNamespace(
        source=SimpleNamespace(platform=Platform(), chat_id="C1", thread_id="123.4"),
        message_id="123.4",
    )
    asyncio.run(
        hc._handle_summary_event(
            SimpleNamespace(),
            event,
            "https://canvas.example/harness?run=hrun_source123&tab=codex",
        )
    )

    command_text = " ".join(captured["argv"])
    assert "--sandbox workspace-write" in command_text
    assert "Preserve the source structure" in command_text
    assert "if a list has N bullets or numbered items, keep N corresponding items" in command_text
    assert any("temporary `ask` mode" in post for post in captured["posts"])
    assert any("Completed the feature" in post for post in captured["posts"])
    final_post = captured["posts"][-1]
    assert "KEY RESULTS" not in final_post
    assert "ACTION ITEMS" not in final_post
    assert "BLOCKERS" not in final_post


def test_hanswer_treats_all_remaining_text_as_question(monkeypatch):
    import plugins.harness_controller as hc

    source_run = SimpleNamespace(
        run_id="hrun_source123",
        task_id="htask_1",
        thread_key="slack:C1:123.4",
        harness="codex",
        model="gpt-5.6-sol",
        workdir="",
        repo="",
        branch="",
        native={"transcript_path": ""},
        links={"canvas": "https://canvas.example/harness?run=hrun_source123"},
    )
    answer_run = SimpleNamespace(run_id="hrun_answer456")
    captured = {"posts": [], "argv": []}

    monkeypatch.setattr(
        hc, "_find_latest_thread_run", lambda thread_key, selector="", exclude_ask=False: source_run
    )
    monkeypatch.setattr(hc, "_read_run_output", lambda run, task=None: "codex\nImplemented A, B, and C.")
    monkeypatch.setattr(hc._controller, "get_task", lambda task_id: (_ for _ in ()).throw(KeyError(task_id)))
    monkeypatch.setattr(hc._store, "load", lambda task_id: (_ for _ in ()).throw(KeyError(task_id)))
    monkeypatch.setattr(hc._run_store, "create_run", lambda **kwargs: answer_run)
    monkeypatch.setattr(hc._run_store, "record_result", lambda *args, **kwargs: answer_run)

    async def fake_run_capture(argv, cwd=None, timeout=600):
        captured["argv"] = argv
        return 0, "codex\nB was verified with the integration test."

    async def fake_post(gateway, event, text, blocks=None):
        captured["posts"].append(text)

    monkeypatch.setattr(hc, "_run_capture", fake_run_capture)
    monkeypatch.setattr(hc, "_post_to_thread", fake_post)

    class Platform:
        value = "slack"

    event = SimpleNamespace(
        source=SimpleNamespace(platform=Platform(), chat_id="C1", thread_id="123.4"),
        message_id="123.4",
    )
    question = (
        "For https://canvas.example/harness?run=hrun_source123&tab=codex, "
        "how exactly was item B verified?"
    )
    asyncio.run(hc._handle_answer_event(SimpleNamespace(), event, question))

    command_text = " ".join(captured["argv"])
    assert question in command_text
    assert "Treat everything in USER QUESTION as the question" in command_text
    assert any("temporary `ask` mode" in post for post in captured["posts"])
    assert captured["posts"][-1] == "B was verified with the integration test."


def test_hanswer_keeps_pending_clarification_compatibility(monkeypatch):
    import plugins.harness_controller as hc

    task = SimpleNamespace(
        task_id="htask_pending",
        harness="codex",
        open_questions=[{"status": "awaiting_user"}],
    )
    posts = []
    captured = {}

    monkeypatch.setattr(hc, "_resolve_answer_target", lambda raw: (task, "use smoke tests"))

    async def fake_continue(task_id, answer, body=None, actor="", notify=None):
        captured.update(task_id=task_id, answer=answer)
        return "continued"

    async def fake_post(gateway, event, text, blocks=None):
        posts.append(text)

    monkeypatch.setattr(hc, "_continue_task_with_answer", fake_continue)
    monkeypatch.setattr(hc, "_post_to_thread", fake_post)
    event = SimpleNamespace(source=SimpleNamespace(user_id="U1"))

    asyncio.run(hc._handle_answer_event(SimpleNamespace(), event, "htask_pending use smoke tests"))

    assert captured == {"task_id": "htask_pending", "answer": "use smoke tests"}
    # Content now reaches Slack through the notify sink during the continuation,
    # so the handler no longer emits a redundant trailing summary post.
    assert posts == []


def _answerable_task(task_id="htask_live", pending=False):
    return SimpleNamespace(
        task_id=task_id,
        harness="codex",
        model="gpt-5.6-sol",
        open_questions=[{"status": "awaiting_user" if pending else "answered"}],
    )


def test_hanswer_continues_live_session_when_no_pending_question(monkeypatch):
    """Regression: !hanswer with no open question must NOT silently downgrade.

    Previously, once the clarification was answered, !hanswer fell through to
    the read-only ask path: it cold-started a fresh harness session, so the
    user's follow-up never reached the session they were watching.
    """
    import plugins.harness_controller as hc

    task = _answerable_task(pending=False)
    posts = []
    captured = {}

    monkeypatch.setattr(hc, "_resolve_answer_target", lambda raw: (task, "resubmit with none"))
    monkeypatch.setattr(hc, "_can_resume_natively", lambda t: True)

    async def fake_continue(task_id, answer, body=None, actor="", notify=None):
        captured.update(task_id=task_id, answer=answer, has_notify=notify is not None)
        if notify:
            await notify("📋 Plan/proposal updated")
        return "continued"

    async def fake_post(gateway, event, text, blocks=None):
        posts.append(text)

    async def fail_question(gateway, event, raw_args):
        raise AssertionError("must not fall through to the read-only ask path")

    monkeypatch.setattr(hc, "_continue_task_with_answer", fake_continue)
    monkeypatch.setattr(hc, "_post_to_thread", fake_post)
    monkeypatch.setattr(hc, "_handle_run_question_event", fail_question)
    event = SimpleNamespace(source=SimpleNamespace(user_id="U1"))

    asyncio.run(hc._handle_answer_event(SimpleNamespace(), event, "htask_live resubmit with none"))

    assert captured["task_id"] == "htask_live"
    assert captured["answer"] == "resubmit with none"
    # The plan content must actually reach the user.
    assert any("Plan/proposal updated" in p for p in posts)
    # And the mode change must be disclosed, not silent.
    assert any("No pending question" in p for p in posts)


def test_hanswer_falls_back_to_ask_when_session_not_resumable(monkeypatch):
    """No live session left: read-only answer is fine, but must be disclosed."""
    import plugins.harness_controller as hc

    task = _answerable_task(pending=False)
    posts = []
    called = {}

    monkeypatch.setattr(hc, "_resolve_answer_target", lambda raw: (task, "summarize status"))
    monkeypatch.setattr(hc, "_can_resume_natively", lambda t: False)

    async def fake_post(gateway, event, text, blocks=None):
        posts.append(text)

    async def fake_question(gateway, event, raw_args):
        called["raw"] = raw_args

    monkeypatch.setattr(hc, "_post_to_thread", fake_post)
    monkeypatch.setattr(hc, "_handle_run_question_event", fake_question)
    event = SimpleNamespace(source=SimpleNamespace(user_id="U1"))

    asyncio.run(hc._handle_answer_event(SimpleNamespace(), event, "htask_live summarize status"))

    assert called["raw"] == "htask_live summarize status"
    assert any("will NOT change the harness session" in p for p in posts)


def test_find_latest_thread_run_skips_ask_runs_for_continuation(monkeypatch):
    """Regression: continuations must not chain off read-only ask runs."""
    import plugins.harness_controller as hc

    plan_run = SimpleNamespace(
        run_id="hrun_plan", task_id="htask_1", thread_key="slack:C1:1.0", mode="plan"
    )
    ask_run = SimpleNamespace(
        run_id="hrun_ask", task_id="htask_1", thread_key="slack:C1:1.0", mode="ask"
    )
    # list_runs() is newest-first, so the ask run shadows the plan run.
    monkeypatch.setattr(hc._run_store, "list_runs", lambda: [ask_run, plan_run])

    assert hc._find_latest_thread_run("slack:C1:1.0").run_id == "hrun_ask"
    assert hc._find_latest_thread_run("slack:C1:1.0", exclude_ask=True).run_id == "hrun_plan"


def test_find_latest_thread_run_matches_task_id_with_trailing_text(monkeypatch):
    """Regression: the selector is 'htask_x <message>', not a bare task id."""
    import plugins.harness_controller as hc

    plan_run = SimpleNamespace(
        run_id="hrun_plan", task_id="htask_1", thread_key="other", mode="plan"
    )
    monkeypatch.setattr(hc._run_store, "list_runs", lambda: [plan_run])

    found = hc._find_latest_thread_run("slack:C9:9.9", "htask_1 we need 100 tokens")
    assert found is not None and found.run_id == "hrun_plan"
