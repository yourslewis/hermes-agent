from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import uuid
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .controller import DecisionLogEntry, HarnessController, _now
from .clarification_blocks import build_clarification_blocks
from .harnesses import build_harness_command, normalize_mode
from .native_transcript import find_claude_session, find_codex_rollout, native_transcript_for_run, render_native_transcript
from .parser import parse_harness_output
from .preferences import HarnessPreferenceStore, HarnessRunArgs, parse_preference_args, preference_summary
from .run_registry import HarnessRunStore
from .slack_blocks import build_plan_blocks
from .slack_actions import acknowledge_slack_action, handle_approve_auto, handle_cancel, post_slack_thread_message
from .store import HarnessTaskStore, apply_parsed_output, build_handoff_packet, build_resume_packet, build_revision_packet, record_question_answer

logger = logging.getLogger(__name__)
_controller = HarnessController.in_memory()
_store = HarnessTaskStore(Path(get_hermes_home()) / "harness_tasks")
_run_store = HarnessRunStore(Path(get_hermes_home()) / "harness_runs")
_pref_store = HarnessPreferenceStore(Path(get_hermes_home()) / "harness_config")
_preferences: dict[str, dict[str, str]] = {}


def _load_profile_config() -> dict[str, Any]:
    config_path = Path(get_hermes_home()) / "config.yaml"
    try:
        import yaml

        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _auto_route_settings() -> dict[str, Any]:
    cfg = _load_profile_config()
    harness_cfg = cfg.get("harness") if isinstance(cfg, dict) else {}
    if not isinstance(harness_cfg, dict):
        return {}
    auto_cfg = harness_cfg.get("auto_route")
    return auto_cfg if isinstance(auto_cfg, dict) else {}


def _auto_route_enabled_for_event(event: Any) -> bool:
    auto_cfg = _auto_route_settings()
    if not auto_cfg:
        return False
    if not bool(auto_cfg.get("enabled", False)):
        return False

    platforms = auto_cfg.get("platforms")
    normalized: set[str] = set()
    if isinstance(platforms, list):
        normalized = {str(x).strip().lower() for x in platforms if str(x).strip()}
    elif isinstance(platforms, str) and platforms.strip():
        normalized = {platforms.strip().lower()}

    if normalized:
        source = getattr(event, "source", None)
        platform = getattr(getattr(source, "platform", None), "value", None) or str(getattr(source, "platform", ""))
        if platform.strip().lower() not in normalized:
            return False
    return True


def _should_bypass_auto_route(text: str) -> bool:
    if not text:
        return True
    # Explicit bypass for direct Hermes chat in auto-routed threads.
    if text.startswith("!!"):
        return True
    # Let slash/command style messages continue through normal command routing.
    if text.startswith("/") or text.startswith("!"):
        return True
    return False


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


def _sanitize_harness_args(raw: str) -> str:
    """Remove Slack mentions from harness command args."""
    text = (raw or "").strip()
    if not text:
        return ""
    text = re.sub(r"<@[A-Z0-9]+>", " ", text)
    text = re.sub(r"@\w+", " ", text)
    return " ".join(text.split())


def _should_temporarily_execute_summary(goal: str) -> bool:
    compact = " ".join((goal or "").strip().lower().split())
    if not compact:
        return False
    hints = ("summary", "summarize", "summarise", "tl;dr", "tldr", "recap", "digest")
    return any(hint in compact for hint in hints)


def _extract_summary_payload(raw_output: str, harness: str) -> dict[str, Any]:
    parsed = parse_harness_output(raw_output or "")
    summary = (parsed.summary or "").strip()
    if not summary:
        summary = (_extract_presentable_reply(raw_output or "", harness) or "").strip()
    if not summary:
        summary = "No concise summary found in the harness output."
    key_results = [x.strip() for x in parsed.evidence if str(x).strip()][:5]
    actions = [x.strip() for x in parsed.actions if str(x).strip()][:5]
    blockers = [q.question for q in parsed.open_questions if getattr(q, "question", "").strip()][:3]
    return {
        "summary": summary,
        "key_results": key_results,
        "actions": actions,
        "blockers": blockers,
        "parsed": parsed,
    }


def _extract_run_id(text: str) -> str:
    match = re.search(r"\b(hrun_[a-zA-Z0-9]+)\b", text or "")
    return match.group(1) if match else ""


def _find_latest_thread_run(
    thread_key: str, selector: str = "", *, exclude_ask: bool = False
) -> Any | None:
    """Newest run matching the selector/thread.

    ``exclude_ask`` skips read-only ``ask`` runs (produced by !hanswer answering
    a question *about* a run). Those are dead ends for continuation: they run in
    a throwaway cold session and carry no plan state, so chaining a follow-up off
    one compounds context drift instead of advancing the real task.
    """
    needle = (selector or "").strip()
    runs = _run_store.list_runs()

    def _ok(run: Any) -> bool:
        if not exclude_ask:
            return True
        return str(getattr(run, "mode", "") or "").strip().lower() != "ask"

    if needle:
        run_id = _extract_run_id(needle)
        if run_id:
            for run in runs:
                if run.run_id == run_id:
                    return run
        task_match = re.match(r"^(htask_[a-zA-Z0-9]+)\b", needle)
        if task_match:
            task_id = task_match.group(1)
            for run in runs:
                if run.task_id == task_id and _ok(run):
                    return run
            for run in runs:
                if run.task_id == task_id:
                    return run
    for run in runs:
        if run.thread_key == thread_key and _ok(run):
            return run
    for run in runs:
        if run.thread_key == thread_key:
            return run
    return None


NATIVE_SECTION_HEADER = "NATIVE SESSION CONTINUATION"
CAPTURED_SECTION_DIVIDER = "\n\n---\n\nCAPTURED RUN OUTPUT (before the native handoff)\n"


def _captured_run_end(run: Any) -> str:
    """Best-effort ISO timestamp for when the Hermes-launched process ended."""
    for attr in ("updated_at", "created_at"):
        value = str(getattr(run, attr, "") or "").strip()
        if value:
            return value
    return ""


def _native_continuation_for(run: Any, task: Any | None = None) -> str:
    """Render native-session turns that happened after Hermes stopped watching.

    Returns "" when there is no native session, nothing newer, or the harness
    is not supported. Never raises: a summary degrading to the captured output
    is far better than a command that errors out.
    """
    try:
        transcript = native_transcript_for_run(run)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Native transcript lookup failed for run %s: %s", getattr(run, "run_id", "?"), exc)
        return ""
    if not transcript:
        return ""
    cutoff = _captured_run_end(run)
    if cutoff and transcript.last_ts and transcript.last_ts <= cutoff:
        # Nothing happened natively after the captured run; the cache is current.
        return ""
    return render_native_transcript(transcript)


def _read_run_output(run: Any, task: Any | None = None) -> str:
    """Return the fullest available output for a run.

    Native-session continuation (work done directly in the harness CLI after
    the Hermes-launched process exited) is placed FIRST: callers slice this
    with a tail window (``[-256000:]``), so appending would let a large stale
    cache push the newest turns out of the prompt entirely.
    """
    native = _native_continuation_for(run, task)

    captured = ""
    if task and task.last_harness_output:
        captured = task.last_harness_output
    else:
        transcript_path = Path(run.native.get("transcript_path") or "")
        if transcript_path.exists():
            lines: list[str] = []
            for line in transcript_path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(line)
                    lines.append(str(item.get("text") or ""))
                except Exception:
                    lines.append(line)
            captured = "\n".join(lines)

    if not native:
        return captured
    if not captured:
        return native
    return f"{native}{CAPTURED_SECTION_DIVIDER}{captured}"


def _native_disclosure_line(run: Any) -> str:
    """One-line note when a run continued outside Hermes' view.

    Without this, a summary can confidently describe a snapshot that is hours
    stale without ever signalling that it might be.
    """
    try:
        transcript = native_transcript_for_run(run)
    except Exception:  # pragma: no cover - defensive
        return ""
    if not transcript:
        return ""
    cutoff = _captured_run_end(run)
    if cutoff and transcript.last_ts and transcript.last_ts <= cutoff:
        return ""
    return (
        f"\n\nℹ️ This run continued in a native `{run.harness}` session "
        f"(last activity {transcript.last_ts or 'unknown'}); "
        f"{len(transcript.turns)} native turns are included above."
    )


async def _handle_summary_event(gateway: Any, event: Any, raw_args: str) -> None:
    selector = _sanitize_harness_args(raw_args)
    thread_key = _thread_key_from_event(event)
    run = _find_latest_thread_run(thread_key, selector)
    if run is None:
        await _post_to_thread(
            gateway,
            event,
            "No harness run found for this thread yet. Start one with `!hrun <task>` first.",
        )
        return

    task = None
    try:
        task = _controller.get_task(run.task_id)
    except Exception:
        try:
            task = _store.load(run.task_id)
            _controller.remember_task(task)
        except Exception:
            task = None

    raw_output = _read_run_output(run, task)
    source_reply = _extract_presentable_reply(raw_output, run.harness) or raw_output
    summary_prompt = (
        f"Condense the completed harness run `{run.run_id}` below.\n"
        "Return only the condensed version. Preserve the source structure, ordering, headings, "
        "and list cardinality: if a list has N bullets or numbered items, keep N corresponding "
        "items and make each one shorter. Do not introduce a new summary template, new sections, "
        "or instructions for future work. Retain concrete outcomes, verification, and follow-ups.\n\n"
        "SOURCE RUN OUTPUT\n"
        f"{source_reply[-256000:]}"
    )
    spec = build_harness_command(
        harness=run.harness,
        model=run.model,
        mode="ask",
        prompt=summary_prompt,
        workdir=run.workdir or None,
    )
    summary_run = _run_store.create_run(
        task_id=run.task_id,
        thread_key=thread_key,
        harness=spec.harness,
        model=run.model,
        mode="ask",
        goal=f"Summarize completed run {run.run_id}",
        workdir=run.workdir,
        repo=run.repo,
        branch=run.branch,
        source=_source_dict_from_event(event),
        argv=spec.argv,
        handoff={"summary_of_run_id": run.run_id, "temporary_mode": "ask"},
    )
    await _post_to_thread(
        gateway,
        event,
        f"Summarizing `{run.run_id}` in temporary `ask` mode. The profile default remains `plan`.",
    )
    code, summary_output = await _run_capture(spec.argv, cwd=spec.cwd, timeout=600)
    _run_store.record_result(summary_run.run_id, exit_code=code, output=summary_output)
    summary = _extract_presentable_reply(summary_output, run.harness) or summary_output.strip()
    if not summary:
        summary = "No summary was returned."
    if code != 0:
        summary = f"⚠️ Summary generation exited with code {code}.\n\n{summary}"
    await _post_to_thread(
        gateway,
        event,
        _truncate_for_slack(summary, limit=2800) + _native_disclosure_line(run),
    )


def _resolve_answer_target(raw_args: str) -> tuple[Any, str] | None:
    """Resolve ``!hanswer <htask_id> <message>`` to a continuable task.

    Deliberately NOT gated on a pending clarification question. Gating on that
    was the original bug: once the single open question was answered, the same
    command silently changed meaning from "continue the live session" to "ask a
    cold model a question about a stale transcript", producing replies that
    never reached the session the user was watching.
    """
    match = re.match(r"^\s*(htask_[a-zA-Z0-9]+)\s+([\s\S]+?)\s*$", raw_args or "")
    if not match:
        return None
    task_id, answer = match.group(1), match.group(2)
    try:
        task = _controller.get_task(task_id)
    except Exception:
        try:
            task = _store.load(task_id)
            _controller.remember_task(task)
        except Exception:
            return None
    return task, answer


def _pending_clarification_task(raw_args: str) -> tuple[Any, str] | None:
    resolved = _resolve_answer_target(raw_args)
    if resolved is None:
        return None
    task, answer = resolved
    if not any(q.get("status") == "awaiting_user" for q in task.open_questions):
        return None
    return task, answer


async def _handle_run_question_event(gateway: Any, event: Any, raw_args: str) -> None:
    question = (raw_args or "").strip()
    if not question:
        await _post_to_thread(
            gateway,
            event,
            "Usage: `!hanswer <your question>` (optionally include a Canvas URL or `hrun_...`).",
        )
        return

    thread_key = _thread_key_from_event(event)
    run = _find_latest_thread_run(thread_key, question, exclude_ask=True)
    if run is None:
        await _post_to_thread(
            gateway,
            event,
            "No harness run found for this thread or question. Include a Canvas URL or `hrun_...`.",
        )
        return

    task = None
    try:
        task = _controller.get_task(run.task_id)
    except Exception:
        try:
            task = _store.load(run.task_id)
            _controller.remember_task(task)
        except Exception:
            task = None

    raw_output = _read_run_output(run, task)
    source_reply = _extract_presentable_reply(raw_output, run.harness) or raw_output
    answer_prompt = (
        f"Answer the user's question about completed harness run `{run.run_id}`.\n"
        "Treat everything in USER QUESTION as the question. Return the answer directly, not a plan "
        "or instructions for how to answer. Preserve any output format explicitly requested by the user.\n\n"
        "USER QUESTION\n"
        f"{question}\n\n"
        "SOURCE RUN OUTPUT\n"
        f"{source_reply[-256000:]}"
    )
    spec = build_harness_command(
        harness=run.harness,
        model=run.model,
        mode="ask",
        prompt=answer_prompt,
        workdir=run.workdir or None,
    )
    answer_run = _run_store.create_run(
        task_id=run.task_id,
        thread_key=thread_key,
        harness=spec.harness,
        model=run.model,
        mode="ask",
        goal=question,
        workdir=run.workdir,
        repo=run.repo,
        branch=run.branch,
        source=_source_dict_from_event(event),
        argv=spec.argv,
        handoff={"question_about_run_id": run.run_id, "temporary_mode": "ask"},
    )
    await _post_to_thread(
        gateway,
        event,
        f"Answering from `{run.run_id}` in temporary `ask` mode. The profile default remains `plan`.",
    )
    code, answer_output = await _run_capture(spec.argv, cwd=spec.cwd, timeout=600)
    _run_store.record_result(answer_run.run_id, exit_code=code, output=answer_output)
    answer = _extract_presentable_reply(answer_output, run.harness) or answer_output.strip()
    if not answer:
        answer = "No answer was returned."
    if code != 0:
        answer = f"⚠️ Answer generation exited with code {code}.\n\n{answer}"
    await _post_to_thread(
        gateway,
        event,
        _truncate_for_slack(answer, limit=2800) + _native_disclosure_line(run),
    )


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
    raw_output = proc.stdout or ""
    updated = _run_store.record_result(run.run_id, exit_code=proc.returncode, output=raw_output)
    presentable = _extract_presentable_reply(raw_output, spec.harness) or raw_output.strip()
    return json.dumps({"success": proc.returncode == 0, "run": asdict(updated), "command": spec.argv, "output": presentable[-4000:]})


def _effective_workdir(spec_cwd: str | None) -> str:
    """The directory the harness process will actually run in.

    ``build_harness_command`` leaves ``cwd`` as None when no workdir was
    requested, and ``create_subprocess_exec(cwd=None)`` then inherits the
    gateway's cwd. Recording '' for that case loses the one fact native resume
    depends on: claude scopes sessions to a *project directory* derived from
    the launch cwd, so a session created here is invisible from anywhere else.
    Storing the resolved path lets Canvas open the right folder instead of
    asking the user to guess (a wrong guess exits 1 and reconnect-loops).
    """
    return str(Path(spec_cwd).expanduser().resolve()) if spec_cwd else os.getcwd()


async def _run_capture(
    argv: list[str],
    cwd: str | None = None,
    timeout: int = 600,
    stdin_text: str | None = None,
) -> tuple[int, str]:
    if not argv:
        return 0, "Hermes is the active/default harness for this profile. No external worker command was launched; continue orchestration in the current Hermes session."
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE if stdin_text is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        # E2BIG ("Argument list too long") and friends must surface as a failed
        # run rather than propagating out and killing the platform action
        # handler, which used to leave a phantom run stuck in state=created.
        logger.error("Harness spawn failed for argv[0]=%s: %s", argv[0], exc)
        return 126, f"Failed to start harness process `{argv[0]}`: {exc}"
    payload = stdin_text.encode("utf-8", errors="replace") if stdin_text is not None else None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(input=payload), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        # The harness session itself is NOT lost here: claude/codex persist to
        # their own session store as they work, so the run stays resumable via
        # the id pinned at launch. Say so, because "timed out" alone reads as
        # total loss and the transcript is otherwise empty.
        return 124, (
            f"Harness command timed out after {timeout}s and was killed.\n"
            "Any native session it created is still on disk and can be resumed; "
            "work started in the cloud (e.g. submitted jobs) keeps running."
        )
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
    agent = (os.environ.get("HERMES_PROFILE") or "").strip()
    return {
        "platform": platform,
        "channel_id": getattr(source, "chat_id", "") or "",
        "thread_ts": getattr(source, "thread_id", None) or getattr(event, "message_id", None) or "",
        "message_id": getattr(event, "message_id", "") or "",
        "agent": agent,
    }


async def _handle_run_event(gateway: Any, event: Any, raw_args: str) -> None:
    try:
        args = _parse_run_args(_sanitize_harness_args(raw_args))
    except Exception as exc:
        await _post_to_thread(gateway, event, str(exc))
        return
    thread_key = _thread_key_from_event(event)
    task = _controller.create_task(thread_key, args.harness, args.model, args.goal)
    effective_mode = args.mode
    temporary_summary_override = False
    if args.mode == "plan" and _should_temporarily_execute_summary(args.goal):
        # Auto-routed summary requests are expected to return the summary, not
        # a planning scaffold. Keep preference unchanged; override this run only.
        effective_mode = "ask"
        temporary_summary_override = True
    # Fresh task, so there is nothing to resume -- but pin an id anyway so the
    # run stays resumable if it is killed before printing one (see #timeout).
    _, new_session_id = _resume_ids_for_task(task)
    spec = build_harness_command(
        harness=args.harness,
        model=args.model,
        mode=effective_mode,
        prompt=args.goal,
        workdir=args.workdir or None,
        new_session_id=new_session_id,
    )
    run = _run_store.create_run(
        task_id=task.task_id,
        thread_key=thread_key,
        harness=spec.harness,
        model=args.model,
        mode=effective_mode,
        goal=args.goal,
        workdir=_effective_workdir(args.workdir or None),
        repo=args.repo,
        branch=args.branch,
        source=_source_dict_from_event(event),
        argv=spec.argv,
        handoff={
            "objective": args.goal,
            "repo": args.repo,
            "branch": args.branch,
            "next_required_behavior": "Plan first; do not execute until approved.",
            "pinned_session_id": new_session_id,
        },
    )
    task.run_id = run.run_id
    task.mode = effective_mode if not temporary_summary_override else args.mode
    task.canvas_url = run.links["canvas"]
    task.remote_cli_url = run.links.get("remote_cli", "")
    task.vscode_url = run.links.get("vscode", "")
    _store.save(task)
    action_label = "Planning" if effective_mode == "plan" else "Executing"
    mode_note = f"Mode: {effective_mode}"
    if temporary_summary_override:
        mode_note += " (temporary override for summary request)"
    await _post_to_thread(
        gateway,
        event,
        (
            f"{action_label} `{args.goal}` with `{args.harness}` / `{args.model}`…\n"
            f"{mode_note}\n"
            f"Repo: {args.repo or '(not set)'}\n"
            f"Workdir: {args.workdir or '(not set)'}\n"
            f"Canvas: {task.canvas_url}\n"
            f"Remote CLI: {task.remote_cli_url or '(not available)'}\n"
            f"VS Code: {task.vscode_url or '(not available)'}"
        ),
    )
    code, output = await _run_capture(spec.argv, cwd=spec.cwd, timeout=1200 if effective_mode == "auto" else 600)
    _run_store.record_result(run.run_id, exit_code=code, output=output)
    if effective_mode in {"auto", "ask"}:
        payload = _extract_summary_payload(output, task.harness)
        apply_parsed_output(task, payload["parsed"], output)
        task.state = "awaiting_clarification" if payload["parsed"].open_questions else "done"
        _store.save(task)
        result_text = _truncate_for_slack(payload["summary"], limit=1800)
        await _post_to_thread(
            gateway,
            event,
            (
                f"{'✅' if code == 0 else '⚠️'} Auto execution completed for `{args.goal}`\n"
                f"Harness: `{args.harness}`\n"
                f"Model: `{args.model}`\n"
                f"Mode: `{effective_mode}`\n"
                f"Exit code: `{code}`\n"
                f"Canvas: {task.canvas_url}\n\n"
                f"{result_text}\n"
                f"{'Default mode remains plan.' if temporary_summary_override else ''}"
            ),
        )
        if payload["parsed"].open_questions:
            await _post_to_thread(
                gateway,
                event,
                f"❓ Harness needs clarification for `{task.task_id}`. Use buttons or `!hanswer {task.task_id} <answer>`.",
            )
        return
    if code != 0:
        output = f"Planning failed with exit code {code}.\n\n{output}"
    plan_text = _extract_presentable_reply(output, task.harness) or output.strip() or "No plan output."
    # Use the extracted plan, not raw harness stdout. Raw stdout includes the
    # full tool transcript (skill dumps, directory listings, DB excerpts) and
    # has previously exceeded ARG_MAX when replayed as the auto prompt.
    auto_prompt = plan_text if plan_text.strip() else args.goal
    _controller.attach_plan(task.task_id, plan_text=plan_text, auto_prompt=auto_prompt)
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


async def _continue_task_with_answer(
    task_id: str,
    answer: str,
    body: dict | None = None,
    actor: str = "",
    notify: Any | None = None,
) -> str:
    """Continue a task with the user's reply.

    ``notify`` is an async ``(text) -> None`` sink used when there is no Slack
    ``body`` (the !hanswer command path). Without it every progress and result
    message below is dropped, so the branch that does the real work is also the
    branch that reports nothing -- the user sees the harness session advance but
    gets no reply.
    """
    try:
        task = _controller.get_task(task_id)
    except KeyError:
        task = _store.load(task_id)
        _controller.remember_task(task)

    async def _say(text: str) -> None:
        if body is not None:
            post_slack_thread_message(body, text)
        elif notify is not None:
            await notify(text)

    try:
        record_question_answer(task, answer, actor=actor)
    except KeyError:
        # No pending question: this is a plain continuation of a live session,
        # not a clarification answer. Log it and carry on rather than failing.
        task.decision_log.append(
            DecisionLogEntry(
                time=_now(),
                signal=f"Follow-up message from {actor or 'user'}.",
                evidence=answer,
                decision="Continue the existing harness session with the user's message.",
                expected_outcome="Harness resumes its own session and responds to the follow-up.",
                action_sent="session_continuation",
            )
        )
        task.state = "planning"
    packet = build_handoff_packet(task, user_answer=answer)
    # auto_prompt always keeps the FULL packet: it is the cold-start fallback
    # if the native session cannot be resumed later.
    task.auto_prompt = packet
    task.state = "planning"
    _store.save(task)
    # When the harness can resume its own session, send only the user's reply.
    # Replaying the packet duplicates context the session already holds and can
    # re-assert a stale plan over work the live session has since completed.
    if _can_resume_natively(task):
        prompt = build_resume_packet(task, user_answer=answer)
    else:
        prompt = packet
    await _say(
        f"✅ Answer recorded: {answer}\n🔁 Continuing planning for `{task.task_id}` "
        f"with `{task.harness}` / `{task.model}`."
    )
    await _launch_plan_continuation(task.task_id, prompt, body, notify=notify)
    _store.save(task)
    return f"Answer recorded for {task.task_id}; continuing planning with {task.harness} / {task.model}."


async def _handle_mode_event(gateway: Any, event: Any, raw_args: str) -> None:
    raw = _sanitize_harness_args(raw_args)
    tokens = shlex.split(raw) if raw else []
    if not tokens:
        await _post_to_thread(
            gateway,
            event,
            "Usage: `!hmode [hrun_xxx|htask_xxx] <plan|ask|auto>`",
        )
        return
    try:
        mode = normalize_mode(tokens[-1])
    except ValueError as exc:
        await _post_to_thread(gateway, event, f"{exc}. Use `plan`, `ask` or `auto`.")
        return
    selector = " ".join(tokens[:-1]).strip()

    thread_key = _thread_key_from_event(event)
    run = _find_latest_thread_run(thread_key, selector)
    if run is None:
        await _post_to_thread(
            gateway,
            event,
            "No harness run found for this thread. Start one with `!hrun <task>` first, "
            "or set the profile default with `!harness mode <plan|ask|auto>`.",
        )
        return

    try:
        task = _controller.get_task(run.task_id)
    except Exception:
        try:
            task = _store.load(run.task_id)
            _controller.remember_task(task)
        except Exception:
            task = None
    if task is None:
        await _post_to_thread(gateway, event, f"Could not load task for run `{run.run_id}`.")
        return

    previous = task.mode or "(inherited: plan)"
    task.mode = mode
    task.updated_at = _now()
    _store.save(task)
    await _post_to_thread(
        gateway,
        event,
        (
            f"Mode for `{task.task_id}` (run `{run.run_id}`): `{previous}` → `{mode}`\n"
            f"Continue with `!hanswer {task.task_id} <message>`."
        ),
    )


async def _handle_answer_event(gateway: Any, event: Any, raw_args: str) -> None:
    resolved = _resolve_answer_target(raw_args)
    if resolved is not None:
        task, answer = resolved
        pending = any(q.get("status") == "awaiting_user" for q in task.open_questions)
        # Continue the live session whenever one exists, even with no pending
        # question -- a follow-up like "resubmit with X" is an instruction to the
        # session, not a question about it. Only fall back to the read-only ask
        # path when there is genuinely no session left to resume.
        if pending or _can_resume_natively(task):
            if not pending:
                await _post_to_thread(
                    gateway,
                    event,
                    f"↩️ No pending question for `{task.task_id}`; continuing the live "
                    f"`{task.harness}` session with your message.",
                )

            async def _notify(text: str) -> None:
                await _post_to_thread(gateway, event, text)

            try:
                message = await _continue_task_with_answer(
                    task.task_id,
                    answer,
                    None,
                    actor=getattr(getattr(event, "source", None), "user_id", ""),
                    notify=_notify,
                )
            except Exception as exc:
                logger.exception("Harness answer continuation failed for %s", task.task_id)
                message = f"⚠️ Continuation failed for `{task.task_id}`: {exc}"
                await _post_to_thread(gateway, event, message)
            return
        await _post_to_thread(
            gateway,
            event,
            f"⚠️ No resumable native session for `{task.task_id}`. Answering read-only "
            "from the last captured output; this will NOT change the harness session.",
        )
    await _handle_run_question_event(gateway, event, raw_args)


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
    if text.startswith("/hmode"):
        raw_args = text[len("/hmode"):].strip()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_handle_mode_event(gateway, event, raw_args))
        else:
            loop.create_task(_handle_mode_event(gateway, event, raw_args))
        return {"action": "skip", "reason": "harness controller handling /hmode"}
    if text.startswith("/hsummary"):
        raw_args = text[len("/hsummary"):].strip()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_handle_summary_event(gateway, event, raw_args))
        else:
            loop.create_task(_handle_summary_event(gateway, event, raw_args))
        return {"action": "skip", "reason": "harness controller handling /hsummary"}

    # Optional profile-level auto-route for normal messages.
    # When enabled, plain thread messages are treated as /hrun goals.
    if _auto_route_enabled_for_event(event) and not _should_bypass_auto_route(text):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(_handle_run_event(gateway, event, text))
        else:
            loop.create_task(_handle_run_event(gateway, event, text))
        return {"action": "skip", "reason": "harness controller auto-routed message to /hrun"}

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


def _extract_copilot_reply(text: str) -> str:
    """Return Copilot CLI's final answer without tool progress or run stats."""
    lines = text.splitlines()

    # Prompt mode appends a fixed run-summary footer after the assistant's
    # answer. Keep it in the raw transcript/Canvas, but never echo it to chat.
    footer_start = len(lines)
    footer_re = re.compile(r"^(?:Changes\s+\+\S+\s+-\S+|Duration\s+\S|Tokens\s+[↑↓]|Resume\s+copilot\b)")
    for idx in range(max(0, len(lines) - 12), len(lines)):
        if footer_re.match(lines[idx].strip()):
            footer_start = idx
            break
    lines = lines[:footer_start]

    # Copilot does not print a final "assistant"/"copilot" marker. Its visible
    # tool stream uses top-level ●/✗ entries followed by indented command and
    # result lines. The final answer begins after the last such entry.
    tool_starts = [
        idx
        for idx, line in enumerate(lines)
        if re.match(r"^[●✗]\s+\S", line)
    ]
    if tool_starts:
        answer_start = tool_starts[-1] + 1
        while answer_start < len(lines):
            line = lines[answer_start]
            if not line.strip() or line.startswith(("  │", "  └")):
                answer_start += 1
                continue
            break
        answer = "\n".join(lines[answer_start:]).strip()
        if answer:
            return answer

    return "\n".join(lines).strip()


def _split_native_and_captured(text: str) -> tuple[str, str]:
    """Split a merged run output into (native_block, captured_block).

    Returns ("", text) when the text is not a merged document.
    """
    if not text.lstrip().startswith(NATIVE_SECTION_HEADER):
        return "", text
    parts = text.split(CAPTURED_SECTION_DIVIDER, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return text, ""


def _extract_presentable_reply(raw_output: str, harness: str = "") -> str:
    """Reduce raw harness stdout to the assistant's final reply.

    Merged outputs (native session continuation + captured stdout) are handled
    specially: the native block is already clean conversation and must be
    preserved whole. Running the stdout marker scan over it would walk past the
    native turns and latch onto the last assistant block of the *captured*
    tail -- i.e. return precisely the stale pre-handoff message that including
    the native session was meant to supersede.
    """
    text = (raw_output or "").replace("\r\n", "\n").strip()
    if not text:
        return ""

    native_block, captured_block = _split_native_and_captured(text)
    if native_block:
        captured_reply = (
            _extract_presentable_reply(captured_block, harness) if captured_block.strip() else ""
        )
        if not captured_reply:
            return native_block.strip()
        return (
            f"{native_block.strip()}"
            f"{CAPTURED_SECTION_DIVIDER}"
            f"{captured_reply.strip()}"
        )

    lines = text.splitlines()
    marker_lines = {"assistant", "codex", "claude", "opencode", "copilot", "hermes"}
    normalized_harness = (harness or "").strip().lower().replace("_", "-")
    if normalized_harness == "copilot":
        return _extract_copilot_reply(text)
    if normalized_harness:
        marker_lines.add(normalized_harness)
        if normalized_harness == "claude-code":
            marker_lines.add("claude")

    candidate = text
    for idx in range(len(lines) - 1, -1, -1):
        marker = lines[idx].strip().lower()
        if marker in marker_lines:
            tail = "\n".join(lines[idx + 1 :]).strip()
            if len(tail) >= 40:
                candidate = tail
                break

    tokens_match = re.search(r"(?im)^tokens used\s*$", candidate)
    if tokens_match:
        before = candidate[: tokens_match.start()].strip()
        after = candidate[tokens_match.end() :].strip()
        if after and len(after) >= max(120, len(before) // 3):
            candidate = after
        elif before:
            candidate = before

    cleaned_lines: list[str] = []
    for line in candidate.splitlines():
        stripped = line.strip()
        if stripped.startswith("[Canvas]"):
            break
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines).strip()

    if cleaned.lower().startswith("assistant\n"):
        cleaned = cleaned.split("\n", 1)[1].lstrip()
    return cleaned


def _native_session_id_for_task(task: Any) -> str:
    """Session id of the task's most recent run, if it can be resumed."""
    run_id = str(getattr(task, "run_id", "") or "")
    if not run_id:
        return ""
    try:
        run = _run_store.load(run_id)
    except Exception:
        return ""
    native = getattr(run, "native", None) or {}
    if not isinstance(native, dict):
        return ""
    return str(native.get("session_id") or "")


RESUMABLE_HARNESSES = {"codex", "claude", "claude-code"}


def _resume_ids_for_task(task: Any) -> tuple[str, str]:
    """Return ``(resume_session_id, new_session_id)`` for a launch.

    Exactly one is ever non-empty. If a prior session is verified to exist we
    resume it; otherwise we mint and pin a fresh id so the run is resumable
    even when it produces no parseable output (timeout, kill, crash). Pinning
    up front is the whole point: a killed process never prints a session id,
    but claude persists the session to disk as it goes, so the work survives
    and only our pointer to it would have been lost.
    """
    session_id = _native_session_id_for_task(task) if _can_resume_natively(task) else ""
    if session_id:
        return session_id, ""
    harness = str(getattr(task, "harness", "") or "").strip().lower()
    # Only claude-code supports choosing the id up front (`--session-id`).
    if harness in {"claude", "claude-code"}:
        return "", str(uuid.uuid4())
    return "", ""


def _can_resume_natively(task: Any) -> bool:
    """True only when the harness supports resume AND the session really exists.

    Verified, not assumed. If we stripped the handoff packet on the belief that
    a session would be resumed and the resume then silently cold-started, the
    run would proceed with no context at all -- strictly worse than the
    duplication we are trying to remove.
    """
    harness = str(getattr(task, "harness", "") or "").strip().lower()
    if harness not in RESUMABLE_HARNESSES:
        return False
    session_id = _native_session_id_for_task(task)
    if not session_id:
        return False
    try:
        if harness in {"claude", "claude-code"}:
            # Claude scopes sessions per project dir, so existence is only
            # meaningful relative to the directory the session was created in.
            # Prefer the workdir recorded on the run over the current process
            # cwd: the gateway may have been restarted or launched elsewhere
            # since, and guessing wrong reports a live session as missing.
            workdir = ""
            run_id = str(getattr(task, "run_id", "") or "")
            if run_id:
                try:
                    workdir = str(getattr(_run_store.load(run_id), "workdir", "") or "")
                except Exception:
                    workdir = ""
            workdir = workdir or str(getattr(task, "workdir", "") or "") or os.getcwd()
            return find_claude_session(session_id, workdir) is not None
        return find_codex_rollout(session_id) is not None
    except Exception:  # pragma: no cover - defensive
        return False


async def _launch_plan_continuation(
    task_id: str,
    prompt: str,
    body: dict | None = None,
    notify: Any | None = None,
) -> None:
    task = _controller.get_task(task_id)

    async def _say(text: str) -> None:
        if body is not None:
            post_slack_thread_message(body, text)
        elif notify is not None:
            await notify(text)

    effective_mode = normalize_mode(task.mode or "plan")
    # Only resume against a session we verified exists; otherwise pin a fresh
    # id so this run stays resumable even if it is killed before printing one.
    session_id, new_session_id = _resume_ids_for_task(task)
    spec = build_harness_command(
        harness=task.harness,
        model=task.model,
        mode=effective_mode,
        prompt=prompt,
        workdir=None,
        session_id=session_id,
        new_session_id=new_session_id,
    )
    run = _run_store.create_run(
        task_id=task.task_id,
        thread_key=task.thread_key,
        harness=spec.harness,
        model=task.model,
        mode=effective_mode,
        goal=task.goal,
        workdir=_effective_workdir(spec.cwd),
        source={},
        argv=spec.argv,
        handoff={
            "objective": task.goal,
            "prior_evidence": task.evidence[-12:],
            "resumed_session_id": session_id,
            "pinned_session_id": new_session_id,
        },
    )
    task.run_id = run.run_id
    task.canvas_url = run.links["canvas"]
    task.remote_cli_url = run.links.get("remote_cli", "")
    task.vscode_url = run.links.get("vscode", "")
    logger.info(
        "Harness plan continuation prepared: task=%s run=%s mode=%s resumed=%s argv=%s",
        task_id, run.run_id, effective_mode, session_id or "no", spec.argv,
    )
    code, output = await _run_capture(spec.argv, cwd=spec.cwd, timeout=1200 if effective_mode == "auto" else 600)
    _run_store.record_result(run.run_id, exit_code=code, output=output)
    if code != 0:
        output = f"Planning continuation failed with exit code {code}.\n\n{output}"
    parsed = parse_harness_output(output)
    apply_parsed_output(task, parsed, output)
    if parsed.open_questions:
        _store.save(task)
        await _say(f"❓ Harness needs more clarification for `{task.task_id}`.")
        return
    plan_text = _extract_presentable_reply(output, task.harness) or output.strip() or "No plan output."
    _controller.attach_plan(task.task_id, plan_text=plan_text, auto_prompt=plan_text.strip() or task.auto_prompt)
    _store.save(task)
    # Post the actual plan, not just a "something happened" notice. The whole
    # point of the continuation is the content it produced.
    await _say(
        f"📋 Plan/proposal updated for `{task.task_id}`:\n\n"
        + _truncate_for_slack(plan_text, limit=2800)
        + _native_disclosure_line(run)
    )


async def _launch_auto(task_id: str, body: dict | None = None) -> None:
    task = _controller.get_task(task_id)
    resume_id, new_session_id = _resume_ids_for_task(task)
    spec = build_harness_command(
        harness=task.harness,
        model=task.model,
        mode="auto",
        prompt=task.auto_prompt or task.goal,
        workdir=None,
        session_id=resume_id,
        new_session_id=new_session_id,
    )
    run = _run_store.create_run(
        task_id=task.task_id,
        thread_key=task.thread_key,
        harness=spec.harness,
        model=task.model,
        mode="auto",
        goal=task.goal,
        workdir=_effective_workdir(spec.cwd),
        source={},
        argv=spec.argv,
        handoff={
            "objective": task.goal,
            "prior_evidence": task.evidence[-12:],
            "next_required_behavior": "Execute approved plan and verify.",
            "resumed_session_id": resume_id,
            "pinned_session_id": new_session_id,
        },
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
    code, output = await _run_capture(
        spec.argv,
        cwd=spec.cwd,
        # AUTO runs execute real work (job submission, builds, cloud polling)
        # and legitimately run long. Any bounded wait the agent issues must fit
        # strictly INSIDE this ceiling -- see the aml-job-ops skill's timeout
        # nesting table. Raising this is not a substitute for that ordering.
        timeout=1800,
        stdin_text=spec.prompt if spec.stdin_prompt else None,
    )
    _run_store.record_result(run.run_id, exit_code=code, output=output)
    result_text = _truncate_for_slack(_extract_presentable_reply(output, task.harness) or output.strip() or "(no output)")
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
                f"Canvas: {task.canvas_url}\n\n"
                f"{result_text}"
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
        lambda raw_args: "Answering from harness context…",
        description="Ask a free-form question about the latest/referenced harness run, or answer a pending clarification",
        args_hint="<question including optional Canvas URL or hrun_xxx>",
    )
    ctx.register_command(
        "hmode",
        lambda raw_args: "Updating harness task mode…",
        description="Set the execution mode for a harness task; later !hanswer continuations use it.",
        args_hint="[hrun_xxx|htask_xxx] <plan|ask|auto>",
    )
    ctx.register_command(
        "hsummary",
        lambda raw_args: "Preparing harness summary…",
        description="Summarize the latest harness run for this thread (or by run/task id).",
        args_hint="[hrun_xxx|htask_xxx]",
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
