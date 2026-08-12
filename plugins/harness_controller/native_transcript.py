"""Read native harness CLI transcripts that Hermes did not capture itself.

Background
----------
Hermes launches an external harness (codex, copilot, ...) as a one-shot
subprocess and records its stdout into ``<run>/transcript.jsonl``. The final
stdout blob is also cached on the task as ``last_harness_output``.

When the user resumes the *native* CLI session directly (via the Canvas
"Resume native" link, i.e. ``codex resume <session_id>``), all subsequent work
happens in the harness's own session store. Hermes never sees it, so
``!hsummary`` / ``!hanswer`` answer from a stale snapshot.

This module resolves and parses those native session files so the controller
can merge them back in.

Codex specifics
---------------
Codex writes JSONL rollouts to ``~/.codex/sessions/<YYYY>/<MM>/<DD>/
rollout-<timestamp>-<session_id>.jsonl``. Relevant line shape::

    {"type": "response_item", "timestamp": "...",
     "payload": {"type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "..."}]}}

Reviewer sessions
-----------------
Codex spawns a *separate* rollout for its approval reviewer sub-agent. Those
files sit in the same directory with adjacent timestamps, and their content is
an assessment dialogue ("treat as untrusted evidence" -> ``{"outcome": ...}``),
not the user's conversation. They must never be folded into a summary: they
are not user dialogue, and re-injecting them into a trusted context would
launder content the reviewer was explicitly told to distrust.

We exclude them two ways (defence in depth):
  1. resolution is keyed strictly on the run's stored ``session_id``, never on
     a time window over the sessions directory;
  2. any message opening with the reviewer preamble is dropped during parsing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Injected by Codex at session start / on resume; not something the user said.
_ENV_CONTEXT_PREFIX = "<environment_context>"

# Opening line of every approval-reviewer prompt. See module docstring.
_REVIEWER_PREAMBLES = (
    "The following is the Codex agent history whose request action you are assessing",
    "The following is the Codex agent history added since your last approval assessment",
)

_KEPT_ROLES = ("user", "assistant")


@dataclass
class NativeTurn:
    ts: str
    role: str
    text: str


@dataclass
class NativeTranscript:
    session_id: str = ""
    path: str = ""
    turns: list[NativeTurn] = field(default_factory=list)
    tool_calls: int = 0
    reasoning_steps: int = 0
    malformed_lines: int = 0

    @property
    def last_ts(self) -> str:
        return self.turns[-1].ts if self.turns else ""

    def __bool__(self) -> bool:
        return bool(self.turns)


def codex_sessions_root(home: Path | str | None = None) -> Path:
    base = Path(home).expanduser() if home else Path.home()
    return base / ".codex" / "sessions"


def find_codex_rollout(session_id: str, root: Path | str | None = None) -> Path | None:
    """Return the rollout file for ``session_id``, or None.

    Matched strictly by session id embedded in the filename. Never by mtime or
    time window -- reviewer rollouts are siblings and would be swallowed.
    """
    sid = (session_id or "").strip()
    if not sid:
        return None
    sessions_root = Path(root) if root else codex_sessions_root()
    if not sessions_root.exists():
        return None
    matches = sorted(sessions_root.glob(f"**/rollout-*-{sid}.jsonl"))
    if not matches:
        return None
    if len(matches) > 1:
        logger.warning("Multiple codex rollouts for session %s; using newest", sid)
        matches.sort(key=lambda p: p.stat().st_mtime)
    return matches[-1]


def claude_projects_root(home: Path | str | None = None) -> Path:
    base = Path(home).expanduser() if home else Path.home()
    return base / ".claude" / "projects"


def claude_project_dir_name(workdir: Path | str) -> str:
    """Claude Code's encoding of a cwd into a project directory name.

    Verified against claude 2.1.81: the path is resolved (``/tmp`` ->
    ``/private/tmp`` on macOS) and then every character that is not
    alphanumeric or a dot becomes ``-``. Note this folds BOTH ``/`` and ``_``
    to ``-`` -- ``/Users/x/cc_rt/work`` becomes ``-Users-x-cc-rt-work``.
    Getting this wrong is the most common cause of a silent cold start,
    because resume is cwd-scoped: the same id is invisible from another dir.
    """
    resolved = Path(workdir).expanduser().resolve()
    return re.sub(r"[^A-Za-z0-9.]", "-", str(resolved))


def find_claude_session(
    session_id: str,
    workdir: Path | str,
    root: Path | str | None = None,
) -> Path | None:
    """Return the transcript file for ``session_id`` under ``workdir``, or None.

    Unlike Codex (one flat global session store), Claude Code partitions
    sessions by project directory. A resume issued from the wrong cwd fails
    with "No conversation found" even though the session file exists on disk,
    so the workdir is part of the lookup key, not an optional hint.

    Deliberately NO cross-directory fallback: a hit under a different project
    dir is not resumable from ``workdir``, and returning it would green-light
    the exact silent cold start this function exists to prevent. Verified
    against claude 2.1.81.
    """
    sid = (session_id or "").strip()
    if not sid or not workdir:
        return None
    projects_root = Path(root) if root else claude_projects_root()
    if not projects_root.exists():
        return None
    candidate = projects_root / claude_project_dir_name(workdir) / f"{sid}.jsonl"
    return candidate if candidate.exists() else None


def _message_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("output") or ""))
            elif isinstance(item, str):
                parts.append(item)
    elif isinstance(content, str):
        parts.append(content)
    return "\n".join(p for p in parts if p).strip()


def _is_reviewer_text(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(p) for p in _REVIEWER_PREAMBLES)


def parse_codex_rollout(path: Path | str) -> NativeTranscript:
    """Parse a Codex rollout JSONL into user/assistant turns.

    Drops: developer messages, injected ``<environment_context>`` turns, and
    approval-reviewer dialogue. Tool calls and reasoning steps are counted
    rather than inlined -- a single run can carry hundreds and would swamp
    any summary budget.
    """
    p = Path(path)
    out = NativeTranscript(path=str(p))
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read native transcript %s: %s", p, exc)
        return out

    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            out.malformed_lines += 1
            continue
        if not isinstance(entry, dict):
            out.malformed_lines += 1
            continue

        etype = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue

        if etype == "session_meta":
            out.session_id = str(payload.get("session_id") or out.session_id)
            continue
        if etype != "response_item":
            continue

        ptype = payload.get("type")
        if ptype == "function_call":
            out.tool_calls += 1
            continue
        if ptype == "reasoning":
            out.reasoning_steps += 1
            continue
        if ptype != "message":
            continue

        role = str(payload.get("role") or "")
        if role not in _KEPT_ROLES:
            continue
        text = _message_text(payload)
        if not text:
            continue
        if role == "user" and text.lstrip().startswith(_ENV_CONTEXT_PREFIX):
            continue
        if _is_reviewer_text(text):
            continue

        out.turns.append(NativeTurn(ts=str(entry.get("timestamp") or ""), role=role, text=text))

    return out


def read_codex_native_transcript(
    session_id: str, root: Path | str | None = None
) -> NativeTranscript:
    path = find_codex_rollout(session_id, root)
    if path is None:
        return NativeTranscript(session_id=session_id)
    parsed = parse_codex_rollout(path)
    parsed.session_id = parsed.session_id or session_id
    return parsed


def render_native_transcript(transcript: NativeTranscript, max_chars: int = 60000) -> str:
    """Render turns as readable text, newest-biased if truncation is needed."""
    if not transcript.turns:
        return ""
    header = [
        "NATIVE SESSION CONTINUATION",
        f"(codex session {transcript.session_id}; work done directly in the CLI after "
        "the Hermes-launched process exited)",
        f"Turns: {len(transcript.turns)} | tool calls: {transcript.tool_calls} | "
        f"reasoning steps: {transcript.reasoning_steps}",
        "",
    ]
    blocks = [f"[{t.ts}] {t.role.upper()}:\n{t.text}" for t in transcript.turns]
    body = "\n\n".join(blocks)
    if len(body) > max_chars:
        body = "…(earlier native turns truncated)…\n\n" + body[-max_chars:]
    return "\n".join(header) + body


def native_transcript_for_run(run: Any, root: Path | str | None = None) -> NativeTranscript:
    """Resolve the native transcript for a HarnessRun-like object."""
    harness = str(getattr(run, "harness", "") or "").strip().lower()
    native = getattr(run, "native", None) or {}
    session_id = str(native.get("session_id") or "") if isinstance(native, dict) else ""
    if not session_id:
        return NativeTranscript()
    if harness != "codex":
        # Other harnesses store sessions elsewhere; not yet supported.
        return NativeTranscript(session_id=session_id)
    return read_codex_native_transcript(session_id, root)
