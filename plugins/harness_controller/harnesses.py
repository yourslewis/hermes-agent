from __future__ import annotations

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
) -> HarnessCommandSpec:
    normalized_harness = harness.strip().lower().replace("_", "-")
    normalized_mode = normalize_mode(mode)
    wrapped = _wrap_prompt(normalized_mode, prompt)

    if normalized_harness in {"claude", "claude-code"}:
        argv = ["claude", "-p", wrapped, "--model", model, "--effort", "max"]
        if normalized_mode == "auto":
            argv += ["--permission-mode", "bypassPermissions", "--dangerously-skip-permissions"]
        elif normalized_mode == "plan":
            argv += ["--permission-mode", "plan"]
        else:
            argv += ["--permission-mode", "default"]
        return HarnessCommandSpec("claude-code", model, normalized_mode, wrapped, argv, workdir)

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
        argv.append(wrapped)
        return HarnessCommandSpec("codex", model, normalized_mode, wrapped, argv, workdir)

    if normalized_harness == "opencode":
        agent = "plan" if normalized_mode == "plan" else "build"
        argv = ["opencode", "run", wrapped, "--model", model, "--agent", agent, "--variant", "max"]
        if workdir:
            argv += ["--dir", workdir]
        if normalized_mode == "auto":
            argv.append("--auto")
        return HarnessCommandSpec("opencode", model, normalized_mode, wrapped, argv, workdir)

    if normalized_harness == "copilot":
        argv = ["copilot", "-p", wrapped, "--model", model, "--reasoning-effort", "xhigh"]
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
