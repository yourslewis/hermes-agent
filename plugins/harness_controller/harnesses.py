from __future__ import annotations

import os
import tempfile
import uuid
from dataclasses import dataclass
from typing import Literal

HarnessMode = Literal["plan", "ask", "auto"]


@dataclass(frozen=True)
class HarnessCommandSpec:
    harness: str
    model: str
    mode: HarnessMode
    prompt: str
    argv: list[str]
    cwd: str | None = None
    # When True the prompt must be fed to the process via stdin rather than
    # argv. Long prompts blow the OS ARG_MAX limit (~1MB on macOS, ~2MB on
    # Linux) and the exec fails with OSError(E2BIG) "Argument list too long".
    stdin_prompt: bool = False


# Conservative ceiling for prompt bytes passed through argv. macOS ARG_MAX is
# 1MB total for argv+environ, so stay well under it.
MAX_ARGV_PROMPT_BYTES = 96_000


def _too_long_for_argv(text: str) -> bool:
    return len(text.encode("utf-8", errors="ignore")) > MAX_ARGV_PROMPT_BYTES


PLAN_PREFIX = """You are in PLAN mode.

Do not edit files.
Do not run destructive commands.
Do not submit expensive jobs.
Inspect read-only state only when needed.

Return:
1. Objective
2. Evidence needed
3. Proposed actions
4. Risks and assumptions
5. Expected success signals
6. Exact auto-execution prompt
7. Conditions requiring escalation

User task:
"""

AUTO_PREFIX = """You are in AUTO execution mode.

Execute the approved task fully. Do not ask the user unless the next action is destructive, public, credential-sensitive, or meaningfully expands cost/scope.

If progress slows, blocks, or results differ from expectation:
1. Stop passive waiting.
2. Inspect live state, logs, status, and resources.
3. Try the smallest safe diagnostic/canary.
4. Compare alternate resources if relevant.
5. Continue with the safest reversible next action.
6. Log evidence, decision, and expected outcome.

Do not submit duplicate expensive full jobs. Do not declare success without real verification evidence.

User task:
"""

ASK_PREFIX = """You are in supervised execution mode.

Proceed with the task, but request approval for destructive, public, credential-sensitive, or cost-expanding actions. If blocked, explain the evidence and the smallest safe next action.

User task:
"""


def _wrap_prompt(mode: HarnessMode, prompt: str) -> str:
    if mode == "plan":
        return PLAN_PREFIX + prompt
    if mode == "auto":
        return AUTO_PREFIX + prompt
    return ASK_PREFIX + prompt


def normalize_mode(mode: str | None) -> HarnessMode:
    raw = (mode or "plan").strip().lower()
    if raw in {"automatic", "autonomous", "execute", "exec"}:
        return "auto"
    if raw in {"manual", "human"}:
        return "ask"
    if raw not in {"plan", "ask", "auto"}:
        raise ValueError(f"invalid harness mode: {mode}")
    return raw  # type: ignore[return-value]


def build_harness_command(
    *,
    harness: str,
    model: str,
    mode: str | None,
    prompt: str,
    workdir: str | None = None,
    session_id: str | None = None,
    new_session_id: str | None = None,
) -> HarnessCommandSpec:
    """Build the argv for a harness invocation.

    ``session_id`` resumes the harness's own session where supported (codex,
    claude-code), preserving its native context. Harnesses without a resume
    path ignore it and start fresh, relying on the caller's handoff packet
    instead.

    ``new_session_id`` pins the id of a *fresh* session (claude-code only) so a
    later resume can target it deterministically instead of depending on the
    id being scraped back out of the harness's output. Ignored when
    ``session_id`` is set.
    """
    normalized_harness = harness.strip().lower().replace("_", "-")
    normalized_mode = normalize_mode(mode)
    wrapped = _wrap_prompt(normalized_mode, prompt)

    if normalized_harness == "hermes":
        return HarnessCommandSpec("hermes", model, normalized_mode, wrapped, [], workdir)

    if normalized_harness in {"claude", "claude-code"}:
        # Claude Code resume semantics, verified against CLI 2.1.81:
        #   * `--resume <id>` must precede `-p`, else the id is eaten as prompt.
        #   * sessions are scoped to the *project directory*, so the same id is
        #     invisible from a different cwd -- workdir is part of the key.
        #   * on an unknown id it prints "No conversation found with session ID"
        #     and exits 1 rather than silently cold-starting.
        # Output stays in text mode: parse_harness_output() expects the prose
        # SUMMARY/CAN STOP sections, and --output-format json would break it.
        argv = ["claude"]
        if session_id:
            argv += ["--resume", session_id]
        elif new_session_id:
            # Pin the id at creation so resume never depends on scraping it
            # back out of free-form output.
            argv += ["--session-id", new_session_id]
        stdin_prompt = _too_long_for_argv(wrapped)
        if stdin_prompt:
            # `claude -p` with no inline prompt reads the prompt from stdin.
            argv += ["-p"]
        else:
            argv += ["-p", wrapped]
        argv += ["--model", model, "--effort", "max"]
        if normalized_mode == "auto":
            argv += ["--permission-mode", "bypassPermissions", "--dangerously-skip-permissions"]
        elif normalized_mode == "plan":
            argv += ["--permission-mode", "plan"]
        else:
            argv += ["--permission-mode", "default"]
        return HarnessCommandSpec("claude-code", model, normalized_mode, wrapped, argv, workdir, stdin_prompt)

    if normalized_harness == "codex":
        argv = ["codex"]
        if normalized_mode == "auto":
            argv += ["--ask-for-approval", "never", "--sandbox", "danger-full-access"]
        elif normalized_mode == "plan":
            argv += ["--ask-for-approval", "on-request", "--sandbox", "read-only"]
        else:
            argv += ["--ask-for-approval", "on-request", "--sandbox", "workspace-write"]
        argv += ["exec", "--skip-git-repo-check", "--model", model]
        if workdir:
            argv += ["--cd", workdir]
        # `codex exec resume <session_id>` continues the harness's own session so
        # the model keeps its native context instead of replaying a handoff
        # packet into a cold process.
        if session_id:
            argv += ["resume", session_id]
        # `codex exec -` reads the prompt from stdin, which avoids ARG_MAX.
        if _too_long_for_argv(wrapped):
            argv.append("-")
            return HarnessCommandSpec("codex", model, normalized_mode, wrapped, argv, workdir, True)
        argv.append(wrapped)
        return HarnessCommandSpec("codex", model, normalized_mode, wrapped, argv, workdir)

    if normalized_harness == "copilot":
        # Route through the LiteLLM BYOK wrapper so Copilot uses the same local
        # model endpoint as the other harnesses (it also picks the correct wire
        # API per model). Falls back to bare `copilot` if the wrapper is absent.
        copilot_bin = os.path.expanduser("~/.local/bin/copilot-litellm")
        if not os.path.isfile(copilot_bin):
            copilot_bin = "copilot"
        effective = wrapped
        if _too_long_for_argv(wrapped):
            # Copilot's -p has no stdin mode, so spill the prompt to a file and
            # point the agent at it rather than blowing ARG_MAX.
            prompt_path = os.path.join(tempfile.mkdtemp(prefix="harness-prompt-"), "prompt.md")
            with open(prompt_path, "w", encoding="utf-8") as fh:
                fh.write(wrapped)
            effective = (
                "Your full instructions are too large to pass inline.\n"
                "Read the complete task prompt from this file first, then execute it:\n"
                f"{prompt_path}"
            )
        argv = [copilot_bin, "--resume", str(uuid.uuid4()), "-p", effective, "--model", model, "--reasoning-effort", "xhigh"]
        if normalized_mode == "auto":
            argv += [
                "--allow-all",
                "--allow-all-tools",
                "--allow-all-paths",
                "--allow-all-urls",
                "--no-ask-user",
                "--autopilot",
            ]
        elif normalized_mode == "plan":
            # Keep Copilot in a non-autonomous planning posture. Exact tool names
            # for --available-tools/--deny-tool vary by Copilot release, so the
            # safe invariant here is prompt-level no-mutation + no broad allow flags.
            argv += ["--no-ask-user"]
        return HarnessCommandSpec("copilot", model, normalized_mode, wrapped, argv, workdir)

    raise ValueError(f"unknown harness: {harness}")
