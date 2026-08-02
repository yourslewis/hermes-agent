from __future__ import annotations

import asyncio
import json
import logging
import shlex
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .controller import DecisionLogEntry, HarnessController, _now
from .clarification_blocks import build_clarification_blocks
from .harnesses import build_harness_command, normalize_mode
from .parser import parse_harness_output
from .preferences import HarnessPreferenceStore, HarnessRunArgs, parse_preference_args, preference_summary
from .run_registry import HarnessRunStore
from .shim import ensure_anthropic_shim
from .slack_blocks import build_plan_blocks
from .slack_actions import acknowledge_slack_action, handle_approve_auto, handle_cancel, post_slack_thread_message
from .store import HarnessTaskStore, apply_parsed_output, build_handoff_packet, build_revision_packet, record_question_answer

logger = logging.getLogger(__name__)
_controller = HarnessController.in_memory()
_store = HarnessTaskStore(Path(get_hermes_home()) / "harness_tasks")
_run_store = HarnessRunStore(Path(get_hermes_home()) / "harness_runs")
_pref_store = HarnessPreferenceStore(Path(get_hermes_home()) / "harness_config")
_preferences: dict[str, dict[str, str]] = {}


def _thread_key_from_event(event: Any) -> str:
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None) or str(getattr(source, "platform", "unknown"))
    chat_id = getattr(source, "chat_id", "") or ""
    thread_id = getattr(source, "thread_id", None) or getattr(event, "message_id", None) or ""
    return f"{platform}:{chat_id}:{thread_id}"


def _handle_harness_command(raw_args: str) -> str:
    parts = shlex.split(raw_args or "")
    if not parts or parts[0] in {"show", "status"}:
        pref = _pref_store.get("default")
        if "default" in _preferences:
            pref = pref.from_dict(_preferences["default"])
        return "Harness preference: " + json.dumps(pref.to_dict(), sort_keys=True)
    if parts[0] in {"reset", "clear"}:
        _preferences.clear()
        _pref_store.clear("default")
        return "Harness preferences cleared."
    pref = parse_preference_args(raw_args, _pref_store.get("default"))
    _preferences["default"] = pref.to_dict()
    _pref_store.set("default", pref)
    return "Harness preference set: " + preference_summary(pref)


def _parse_run_args(raw: str) -> HarnessRunArgs:
    tokens = shlex.split(raw)
    pref = _pref_store.get("default")
    if "default" in _preferences:
        pref = pref.from_dict(_preferences["default"])
    harness = pref.harness
    model = pref.model
    mode = pref.mode
    repo = pref.repo
    workdir = pref.workdir
    branch = pref.branch
    goal_parts: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--harness" and i + 1 < len(tokens):
            harness = tokens[i + 1]
            i += 2
        elif token == "--model" and i + 1 < len(tokens):
            model = tokens[i + 1]
            i += 2
        elif token == "--mode" and i + 1 < len(tokens):
            mode = normalize_mode(tokens[i + 1])
            i += 2
        elif token == "--repo" and i + 1 < len(tokens):
            repo = tokens[i + 1]
            i += 2
        elif token in {"--workdir", "--cwd", "--folder"} and i + 1 < len(tokens):
            workdir = tokens[i + 1]
            i += 2
        elif token == "--branch" and i + 1 < len(tokens):
            branch = tokens[i + 1]
            i += 2
        else:
            goal_parts.append(token)
            i += 1
    goal = " ".join(goal_parts).strip()
    if not goal:
        raise ValueError("Usage: /hrun [--harness H] [--model M] [--mode plan|ask|auto] [--repo URL] [--workdir DIR] [--branch BRANCH] <task>")
    return HarnessRunArgs(harness=harness, model=model, mode=mode, goal=goal, workdir=workdir, repo=repo, branch=branch)


def _parse_answer_args(raw: str) -> tuple[str, str]:
    tokens = shlex.split(raw)
    if len(tokens) < 2:
        raise ValueError("Usage: /hanswer <task_id> <answer>")
    return tokens[0], " ".join(tokens[1:]).strip()


def _tool_harness_config(args: dict | None = None, **_: Any) -> str:
    args = args or {}
    action = str(args.get("action") or "show").lower()
    key = str(args.get("key") or "default")
    if action in {"reset", "clear"}:
        _pref_store.clear(key)
        _preferences.pop(key, None)
        return json.dumps({"success": True, "message": f"Harness preference cleared for {key}."})
    if action in {"set", "update"}:
        raw_parts = ["set"]
        for opt in ["harness", "model", "mode", "repo", "workdir", "branch"]:
            value = args.get(opt)
            if value:
                raw_parts.extend([f"--{opt}", str(value)])
        pref = parse_preference_args(" ".join(shlex.quote(x) for x in raw_parts), _pref_store.get(key))
        _pref_store.set(key, pref)
        _preferences[key] = pref.to_dict()
        return json.dumps({"success": True, "preference": pref.to_dict(), "message": preference_summary(pref)})
    pref = _pref_store.get(key)
    if key in _preferences:
        pref = pref.from_dict(_preferences[key])
    return json.dumps({"success": True, "preference": pref.to_dict(), "message": preference_summary(pref)})


def _tool_harness_run(args: dict | None = None, **_: Any) -> str:
    args = args or {}
    goal = str(args.get("goal") or args.get("prompt") or "").strip()
    if not goal:
        return json.dumps({"success": False, "error": "Missing required goal/prompt."})
    pref = _pref_store.get(str(args.get("key") or "default"))
    parsed = HarnessRunArgs(
        harness=str(args.get("harness") or pref.harness),
        model=str(args.get("model") or pref.model),
        mode=normalize_mode(str(args.get("mode") or pref.mode)),
        goal=goal,
        workdir=str(args.get("workdir") or pref.workdir),
        repo=str(args.get("repo") or pref.repo),
        branch=str(args.get("branch") or pref.branch),
    )
    spec = build_harness_command(
        harness=parsed.harness,
        model=parsed.model,
        mode=parsed.mode,
        prompt=parsed.goal,
        workdir=parsed.workdir or None,
    )
    run = _run_store.create_run(
        task_id=str(args.get("task_id") or "tool"),
        thread_key=str(args.get("thread_key") or "tool"),
        harness=spec.harness,
        model=parsed.model,
        mode=parsed.mode,
        goal=parsed.goal,
        workdir=parsed.workdir,
        repo=parsed.repo,
        branch=parsed.branch,
        source={"platform": "tool"},
        argv=spec.argv,
        handoff={"objective": parsed.goal, "repo": parsed.repo, "branch": parsed.branch},
    )
    if bool(args.get("dry_run", False)):
        return json.dumps({"success": True, "run": asdict(run), "command": spec.argv, "dry_run": True})
    if not spec.argv:
        output = "Hermes is the active/default harness for this profile. No external worker command was launched."
        updated = _run_store.record_result(run.run_id, exit_code=0, output=output)
        return json.dumps({"success": True, "run": asdict(updated), "command": spec.argv, "output": output})
    proc = subprocess.run(spec.argv, cwd=spec.cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=int(args.get("timeout") or 600))
    updated = _run_store.record_result(run.run_id, exit_code=proc.returncode, output=proc.stdout or "")
    return json.dumps({"success": proc.returncode == 0, "run": asdict(updated), "command": spec.argv, "output": (proc.stdout or "")[-4000:]})


async def _run_capture(argv: list[str], cwd: str | None = None, timeout: int = 600) -> tuple[int, str]:
    if not argv:
        return 0, "Hermes is the active/default harness for this profile. No external worker command was launched; continue orchestration in the current Hermes session."
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "Harness command timed out."
    return proc.returncode or 0, (stdout or b"").decode(errors="replace")


async def _post_to_thread(gateway: Any, event: Any, text: str, blocks: list[dict] | None = None) -> None:
    source = getattr(event, "source", None)
    adapter = getattr(gateway, "adapters", {}).get(getattr(source, "platform", None))
    chat_id = getattr(source, "chat_id", None)
    if not adapter or not chat_id:
        return
    thread_id = getattr(source, "thread_id", None) or getattr(event, "message_id", None)
    if blocks and hasattr(adapter, "_get_client"):
        try:
            await adapter._get_client(chat_id).chat_postMessage(
                channel=chat_id,
                text=text,
                blocks=blocks,
                **({"thread_ts": thread_id} if thread_id else {}),
            )
            return
        except Exception as exc:  # pragma: no cover - fallback path
            logger.warning("Harness Slack block post failed, falling back to text: %s", exc)
    await adapter.send(chat_id, text, metadata={"thread_id": thread_id} if thread_id else None)


def _source_dict_from_event(event: Any) -> dict[str, Any]:
    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", None) or str(getattr(source, "platform", ""))
    return {
        "platform": platform,
        "channel_id": getattr(source, "chat_id", "") or "",
        "thread_ts": getattr(source, "thread_id", None) or getattr(event, "message_id", None) or "",
        "message_id": getattr(event, "message_id", "") or "",
    }


async def _handle_run_event(gateway: Any, event: Any, raw_args: str) -> None:
    try:
        args = _parse_run_args(raw_args)
    except Exception as exc:
        await _post_to_thread(gateway, event, str(exc))
        return
    thread_key = _thread_key_from_event(event)
    task = _controller.create_task(thread_key, args.harness, args.model, args.goal)
    if args.harness.strip().lower().replace("_", "-") in {"claude", "claude-code"}:
        ensure_anthropic_shim()
    spec = build_harness_command(harness=args.harness, model=args.model, mode="plan", prompt=args.goal, workdir=args.workdir or None)
    run = _run_store.create_run(
        task_id=task.task_id,
        thread_key=thread_key,
        harness=spec.harness,
        model=args.model,
        mode=args.mode,
        goal=args.goal,
        workdir=args.workdir,
        repo=args.repo,
        branch=args.branch,
        source=_source_dict_from_event(event),
        argv=spec.argv,
        handoff={"objective": args.goal, "repo": args.repo, "branch": args.branch, "next_required_behavior": "Plan first; do not execute until approved."},
    )
    task.run_id = run.run_id
    task.canvas_url = run.links["canvas"]
    task.remote_cli_url = run.links.get("remote_cli", "")
    task.vscode_url = run.links.get("vscode", "")
    _store.save(task)
    await _post_to_thread(gateway, event, f"Planning `{args.goal}` with `{args.harness}` / `{args.model}`…\nRepo: {args.repo or '(not set)'}\nWorkdir: {args.workdir or '(not set)'}\nCanvas: {task.canvas_url}\nRemote CLI: {task.remote_cli_url or '(not available)'}\nVS Code: {task.vscode_url or '(not available)'}")
    code, output = await _run_capture(spec.argv, cwd=spec.cwd, timeout=600)
    _run_store.record_result(run.run_id, exit_code=code, output=output)
    if code != 0:
        output = f"Planning failed with exit code {code}.\n\n{output}"
    auto_prompt = output if output.strip() else args.goal
    _controller.attach_plan(task.task_id, plan_text=output.strip() or "No plan output.", auto_prompt=auto_prompt)
    task = _controller.get_task(task.task_id)
    parsed = parse_harness_output(output)
    apply_parsed_output(task, parsed, output)
    _store.save(task)
    if parsed.open_questions:
        await _post_to_thread(
            gateway,
            event,
            f"Harness needs clarification for `{args.goal}` using `{args.harness}` / `{args.model}`.",
            blocks=build_clarification_blocks(task),
        )
        return
    await _post_to_thread(
        gateway,
        event,
        f"Plan ready for `{args.goal}` using `{args.harness}` / `{args.model}`.",
        blocks=build_plan_blocks(task),
    )


async def _continue_task_with_answer(task_id: str, answer: str, body: dict | None = None, actor: str = "") -> str:
    try:
        task = _controller.get_task(task_id)
    except KeyError:
        task = _store.load(task_id)
        _controller.remember_task(task)
    record_question_answer(task, answer, actor=actor)
    packet = build_handoff_packet(task, user_answer=answer)
    task.auto_prompt = packet
    task.state = "planning"
    _store.save(task)
    if body is not None:
        post_slack_thread_message(
            body,
            f"✅ Answer recorded: {answer}\n🔁 Continuing planning for `{task.task_id}` with `{task.harness}` / `{task.model}`.",
        )
    await _launch_plan_continuation(task.task_id, packet, body)
    _store.save(task)
    return f"Answer recorded for {task.task_id}; continuing planning with {task.harness} / {task.model}."


async def _handle_answer_event(gateway: Any, event: Any, raw_args: str) -> None:
    try:
        task_id, answer = _parse_answer_args(raw_args)
        message = await _continue_task_with_answer(task_id, answer, None, actor=getattr(getattr(event, "source", None), "user_id", ""))
    except Exception as exc:
        message = str(exc)
    await _post_to_thread(gateway, event, message)


def _pre_gateway_dispatch(event: Any = None, gateway: Any = None, **_: Any) -> dict | None:
    text = (getattr(event, "text", "") or "").strip()
    if text.startswith("/hrun"):
        raw_args = text[len("/hrun"):].strip()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_handle_run_event(gateway, event, raw_args))
        else:
            loop.create_task(_handle_run_event(gateway, event, raw_args))
        return {"action": "skip", "reason": "harness controller handling /hrun"}
    if text.startswith("/hanswer"):
        raw_args = text[len("/hanswer"):].strip()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_handle_answer_event(gateway, event, raw_args))
        else:
            loop.create_task(_handle_answer_event(gateway, event, raw_args))
        return {"action": "skip", "reason": "harness controller handling /hanswer"}
    if not text.startswith("/harness"):
        return None
    # We cannot synchronously send a Slack button card from this hook; let the
    # registered slash command return text. The hook stays as an extension point
    # for future context-aware /run interception.
    return None


def _truncate_for_slack(text: str, limit: int = 2800) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def _launch_plan_continuation(task_id: str, prompt: str, body: dict | None = None) -> None:
    task = _controller.get_task(task_id)
    spec = build_harness_command(
        harness=task.harness,
        model=task.model,
        mode="plan",
        prompt=prompt,
        workdir=None,
    )
    run = _run_store.create_run(
        task_id=task.task_id,
        thread_key=task.thread_key,
        harness=spec.harness,
        model=task.model,
        mode="plan",
        goal=task.goal,
        workdir=spec.cwd,
        source={},
        argv=spec.argv,
        handoff={"objective": task.goal, "prior_evidence": task.evidence[-12:]},
    )
    task.run_id = run.run_id
    task.canvas_url = run.links["canvas"]
    task.remote_cli_url = run.links.get("remote_cli", "")
    task.vscode_url = run.links.get("vscode", "")
    logger.info("Harness plan continuation prepared: task=%s run=%s argv=%s", task_id, run.run_id, spec.argv)
    code, output = await _run_capture(spec.argv, cwd=spec.cwd, timeout=600)
    _run_store.record_result(run.run_id, exit_code=code, output=output)
    if code != 0:
        output = f"Planning continuation failed with exit code {code}.\n\n{output}"
    parsed = parse_harness_output(output)
    apply_parsed_output(task, parsed, output)
    if parsed.open_questions:
        _store.save(task)
        if body is not None:
            post_slack_thread_message(body, f"❓ Harness needs more clarification for `{task.task_id}`.")
        return
    _controller.attach_plan(task.task_id, plan_text=output.strip() or "No plan output.", auto_prompt=output.strip() or task.auto_prompt)
    _store.save(task)
    if body is not None:
        post_slack_thread_message(body, f"Plan/proposal updated for `{task.task_id}`.")


async def _launch_auto(task_id: str, body: dict | None = None) -> None:
    task = _controller.get_task(task_id)
    spec = build_harness_command(
        harness=task.harness,
        model=task.model,
        mode="auto",
        prompt=task.auto_prompt or task.goal,
        workdir=None,
    )
    run = _run_store.create_run(
        task_id=task.task_id,
        thread_key=task.thread_key,
        harness=spec.harness,
        model=task.model,
        mode="auto",
        goal=task.goal,
        workdir=spec.cwd,
        source={},
        argv=spec.argv,
        handoff={"objective": task.goal, "prior_evidence": task.evidence[-12:], "next_required_behavior": "Execute approved plan and verify."},
    )
    task.run_id = run.run_id
    task.canvas_url = run.links["canvas"]
    task.remote_cli_url = run.links.get("remote_cli", "")
    task.vscode_url = run.links.get("vscode", "")
    logger.info("Harness auto launch prepared: task=%s run=%s argv=%s", task_id, run.run_id, spec.argv)
    if body is not None:
        post_slack_thread_message(
            body,
            f"🚀 Auto execution started\nHarness: `{task.harness}`\nModel: `{task.model}`\nTask: {task.goal}\nCanvas: {task.canvas_url}",
        )
    code, output = await _run_capture(spec.argv, cwd=spec.cwd, timeout=1200)
    _run_store.record_result(run.run_id, exit_code=code, output=output)
    result_text = _truncate_for_slack(output.strip() or "(no output)")
    parsed = parse_harness_output(output)
    apply_parsed_output(task, parsed, output)
    if body is not None:
        status = "✅" if code == 0 else "⚠️"
        post_slack_thread_message(
            body,
            (
                f"{status} Auto execution completed\n"
                f"Harness: `{task.harness}`\n"
                f"Model: `{task.model}`\n"
                f"Exit code: `{code}`\n\n"
                f"```\n{result_text}\n```"
            ),
        )
        if parsed.open_questions:
            post_slack_thread_message(
                body,
                f"❓ Harness needs clarification for `{task.task_id}`. Use the clarification buttons or type `!hanswer {task.task_id} <answer>`."
            )
    task.decision_log.append(
        DecisionLogEntry(
            time=_now(),
            signal=f"Auto harness process exited with code {code}.",
            evidence=(output or "")[-1000:],
            decision="Recorded auto execution result.",
            expected_outcome="User can inspect the posted auto result in Slack.",
            action_sent="auto_result",
        )
    )
    _store.save(task)


async def _launch_terminal_action(task_id: str, body: dict | None = None) -> None:
    task = _controller.get_task(task_id)
    task.state = "running_or_creating"
    _store.save(task)
    if task.approval_action == "launch_auto":
        await _launch_auto(task_id, body)
        return
    if task.approval_action == "create_cron":
        if body is not None:
            post_slack_thread_message(
                body,
                f"🧭 Cron creation requested for `{task.task_id}`. Use Hermes cron creation with the approved instruction.\n\n```\n{_truncate_for_slack(task.auto_prompt or task.plan_text)}\n```",
            )
        task.state = "done"
        _store.save(task)
        return
    if body is not None:
        post_slack_thread_message(body, f"✅ Prompt accepted for `{task.task_id}`.")
    task.state = "done"
    _store.save(task)


async def _on_approve(ack, body, action):
    async def _launch(task_id: str) -> None:
        await _launch_terminal_action(task_id, body)

    await handle_approve_auto(
        ack=ack,
        body=body,
        action=action,
        controller=_controller,
        launch_auto=_launch,
        post_response=lambda body, message: acknowledge_slack_action(body, f"✅ {message}"),
    )


async def _on_cancel(ack, body, action):
    await handle_cancel(
        ack=ack,
        body=body,
        action=action,
        controller=_controller,
        post_response=lambda body, message: acknowledge_slack_action(body, f"🛑 {message}"),
    )


async def _on_answer_choice(ack, body, action):
    await ack()
    raw = str((action or {}).get("value") or "")
    parts = raw.split("|", 3)
    if len(parts) < 4:
        acknowledge_slack_action(body, "⚠️ Invalid harness answer payload.")
        return
    task_id, _question_id, choice_id, label = parts
    actor = str(((body or {}).get("user") or {}).get("id") or "")
    acknowledge_slack_action(body, f"✅ Selected: {label}")
    await _continue_task_with_answer(task_id, f"{choice_id}: {label}", body, actor=actor)


async def _on_answer_other(ack, body, action):
    await ack()
    raw = str((action or {}).get("value") or "")
    task_id = raw.split("|", 1)[0] if raw else "<task_id>"
    acknowledge_slack_action(
        body,
        f"✍️ Freeform answer requested. Reply in this thread with `!hanswer {task_id} <your answer>`.",
    )


def register(ctx):
    ctx.register_tool(
        name="harness_config",
        toolset="harness",
        description="Show, set, or clear the default external harness configuration, including repo/workdir/branch.",
        schema={
            "type": "function",
            "function": {
                "name": "harness_config",
                "description": "Show, set, or clear the default external harness configuration used by /hrun and harness_run.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["show", "set", "reset"], "default": "show"},
                        "key": {"type": "string", "default": "default"},
                        "harness": {"type": "string"},
                        "model": {"type": "string"},
                        "mode": {"type": "string", "enum": ["plan", "ask", "auto"]},
                        "repo": {"type": "string"},
                        "workdir": {"type": "string"},
                        "branch": {"type": "string"},
                    },
                },
            },
        },
        handler=_tool_harness_config,
    )
    ctx.register_tool(
        name="harness_run",
        toolset="harness",
        description="Create and optionally execute a harness run using the stored harness configuration.",
        schema={
            "type": "function",
            "function": {
                "name": "harness_run",
                "description": "Create and optionally execute a Codex/Claude Code/OpenCode/Copilot harness run with repo/workdir/branch metadata and Canvas link.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string"},
                        "prompt": {"type": "string"},
                        "key": {"type": "string", "default": "default"},
                        "harness": {"type": "string"},
                        "model": {"type": "string"},
                        "mode": {"type": "string", "enum": ["plan", "ask", "auto"]},
                        "repo": {"type": "string"},
                        "workdir": {"type": "string"},
                        "branch": {"type": "string"},
                        "dry_run": {"type": "boolean", "default": False},
                        "timeout": {"type": "integer", "default": 600},
                    },
                },
            },
        },
        handler=_tool_harness_run,
    )
    ctx.register_command(
        "harness",
        _handle_harness_command,
        description="Select external harness/model/mode for supervised tasks",
        args_hint="<harness> <model> [plan|ask|auto]",
    )
    ctx.register_command(
        "hrun",
        lambda raw_args: "Starting harness task…",
        description="Run a supervised external harness task (handled by gateway hook)",
        args_hint="[--harness H] [--model M] [--mode plan|ask|auto] <task>",
    )
    ctx.register_command(
        "hanswer",
        lambda raw_args: "Recording harness answer…",
        description="Answer a pending harness clarification and continue the same task",
        args_hint="<task_id> <answer>",
    )
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
    ctx.register_slack_action_handler("harness_approve", _on_approve)
    ctx.register_slack_action_handler("harness_approve_auto", _on_approve)
    ctx.register_slack_action_handler("harness_cancel", _on_cancel)
    for idx in range(4):
        ctx.register_slack_action_handler(f"harness_answer_choice_{idx}", _on_answer_choice)
    ctx.register_slack_action_handler("harness_answer_other", _on_answer_other)
    # Revise is intentionally registered as a no-op placeholder until the
    # revision text capture path is implemented.
    async def _on_revise(ack, body, action):
        await ack()
        task_id = str((action or {}).get("value") or "")
        try:
            task = _controller.get_task(task_id)
        except KeyError:
            task = _store.load(task_id)
            _controller.remember_task(task)
        actor = str(((body or {}).get("user") or {}).get("id") or "")
        result = _controller.request_revision(task_id, feedback="User clicked revise; ask for revision details or produce a safer revised proposal.", actor=actor)
        acknowledge_slack_action(body, f"🔁 {result.message} Reply with `!hanswer {task_id} <revision feedback>` or continue with a revised planning prompt.")
        packet = build_revision_packet(task, feedback="User requested revision from Slack button.")
        await _launch_plan_continuation(task_id, packet, body)
    ctx.register_slack_action_handler("harness_revise_plan", _on_revise)
