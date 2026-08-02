from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CANVAS_ORIGIN = "https://canvas.wenhao.dev"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return "hrun_" + uuid.uuid4().hex[:26]


@dataclass
class HarnessRun:
    run_id: str
    task_id: str
    thread_key: str
    harness: str
    model: str
    mode: str
    goal: str
    workdir: str = ""
    repo: str = ""
    branch: str = ""
    state: str = "created"
    source: dict[str, Any] = field(default_factory=dict)
    git: dict[str, Any] = field(default_factory=dict)
    native: dict[str, Any] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)
    links: dict[str, str] = field(default_factory=dict)
    handoff: dict[str, Any] = field(default_factory=dict)
    exit_code: int | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)


def normalize_harness(harness: str) -> str:
    h = (harness or "").strip().lower().replace("_", "-")
    if h in {"hermes", "self"}:
        return "hermes"
    if h == "claude":
        return "claude-code"
    return h


def canvas_url(run_id: str, harness: str) -> str:
    tab = normalize_harness(harness)
    return f"{CANVAS_ORIGIN}/harness?run={run_id}&tab={tab}"


def remote_cli_url(harness: str) -> str:
    h = normalize_harness(harness)
    routes = {
        "claude-code": "claude-code-cli",
        "codex": "codex-cli",
        "opencode": "opencode-cli",
    }
    route = routes.get(h)
    return f"{CANVAS_ORIGIN}/ui/{route}/" if route else ""


def vscode_url(workdir: str) -> str:
    if not workdir:
        return f"{CANVAS_ORIGIN}/ui/vscode/"
    return f"{CANVAS_ORIGIN}/ui/vscode/?folder={quote(workdir, safe='')}"


def _cd_prefix(workdir: str) -> str:
    return f"cd {workdir} && " if workdir else ""


def resume_command(harness: str, session_id: str | None, workdir: str = "") -> str:
    h = normalize_harness(harness)
    sid = session_id or "<session_id>"
    prefix = _cd_prefix(workdir)
    if h == "claude-code":
        return f"{prefix}claude --resume {sid}"
    if h == "opencode":
        return f"{prefix}opencode -s {sid}"
    if h == "codex":
        return f"{prefix}codex resume {sid}"
    if h == "hermes":
        return f"{prefix}hermes chat -q '<handoff prompt>'"
    if h == "copilot":
        return f"{prefix}copilot -p '<handoff prompt>' --model <model>"
    return f"{prefix}{h} resume {sid}"


def fork_command(harness: str, session_id: str | None, workdir: str = "") -> str:
    h = normalize_harness(harness)
    sid = session_id or "<session_id>"
    prefix = _cd_prefix(workdir)
    if h == "claude-code":
        return f"{prefix}claude --resume {sid} --fork-session"
    if h == "opencode":
        return f"{prefix}opencode -s {sid} --fork"
    if h == "codex":
        return f"{prefix}codex fork {sid}"
    if h == "hermes":
        return f"{prefix}hermes chat -q '<fork handoff prompt>'"
    return ""


def detect_native_session_id(harness: str, output: str) -> str | None:
    text = output or ""
    # Structured JSON/JSONL from Claude Code and Codex commonly includes session_id.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or "session" not in stripped.lower():
            continue
        try:
            data = json.loads(stripped)
        except Exception:
            data = None
        if isinstance(data, dict):
            value = data.get("session_id") or data.get("sessionId") or data.get("sessionID")
            if isinstance(value, str) and value.strip():
                return value.strip()
    # General JSON-ish fallback.
    m = re.search(r'"session[_-]?id"\s*:\s*"([^"]+)"', text, re.I)
    if m:
        return m.group(1).strip()
    # UUID fallback for Claude/Codex session ids near session labels.
    m = re.search(r'(?:session(?:[_ -]?id)?|conversation)\D{0,30}([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', text, re.I)
    if m:
        return m.group(1).strip()
    # OpenCode commonly uses ses_ identifiers.
    m = re.search(r'\b(ses_[A-Za-z0-9][A-Za-z0-9_-]{4,})\b', text)
    if m:
        return m.group(1).strip()
    return None


class HarnessRunStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.runs_root = self.root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.runs_root / run_id

    def create_run(
        self,
        *,
        task_id: str,
        thread_key: str,
        harness: str,
        model: str,
        mode: str,
        goal: str,
        workdir: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        source: dict[str, Any] | None = None,
        argv: list[str] | None = None,
        handoff: dict[str, Any] | None = None,
    ) -> HarnessRun:
        h = normalize_harness(harness)
        run_id = _new_run_id()
        d = self.run_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        native = {
            "session_id": "",
            "process_id": None,
            "tmux_session": "",
            "app_server_url": "",
            "acp_endpoint": "",
            "transcript_path": str(d / "transcript.jsonl"),
            "log_path": str(d / "process.log"),
        }
        commands = {
            "launch": " ".join(argv or []),
            "resume": resume_command(h, None, workdir or ""),
            "fork": fork_command(h, None, workdir or ""),
        }
        links = {
            "canvas": canvas_url(run_id, h),
            "remote_cli": remote_cli_url(h),
            "vscode": vscode_url(workdir or ""),
        }
        run = HarnessRun(
            run_id=run_id,
            task_id=task_id,
            thread_key=thread_key,
            harness=h,
            model=model,
            mode=mode,
            goal=goal,
            workdir=workdir or "",
            repo=repo or "",
            branch=branch or "",
            state="created",
            source=source or {},
            native=native,
            commands=commands,
            links=links,
            handoff=handoff or {},
        )
        self.save(run)
        self.append_event(run_id, "created", {"argv": argv or []})
        return run

    def save(self, run: HarnessRun) -> None:
        run.updated_at = _now()
        d = self.run_dir(run.run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "run.json").write_text(json.dumps(asdict(run), indent=2, sort_keys=True), encoding="utf-8")

    def load(self, run_id: str) -> HarnessRun:
        data = json.loads((self.run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
        return HarnessRun(**data)

    def list_runs(self) -> list[HarnessRun]:
        runs: list[HarnessRun] = []
        if not self.runs_root.exists():
            return runs
        for path in sorted(self.runs_root.glob("*/run.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                runs.append(HarnessRun(**data))
            except Exception:
                continue
        runs.sort(key=lambda r: r.updated_at or r.created_at, reverse=True)
        return runs

    def list_grouped_by_agent(self, pref_store: Any | None = None) -> dict[str, Any]:
        """Return dashboard-ready harness runs grouped by agent.

        Shape is intentionally simple for the dashboard harness tab:
        agents -> default harness first, then thread summaries ordered by the
        latest run/message timestamp, newest first.
        """
        def _default_harness_for(agent_name: str) -> dict[str, str]:
            pref = None
            if pref_store is not None:
                key = agent_name.strip().lower().replace(" ", "-") or "default"
                default_pref = pref_store.get("default")
                candidate = pref_store.get(key) if key != "default" else default_pref
                built_in_default = type(default_pref)()
                pref = default_pref if key != "default" and candidate == built_in_default else candidate
            return {
                "harness": getattr(pref, "harness", ""),
                "model": getattr(pref, "model", ""),
                "mode": getattr(pref, "mode", ""),
                "repo": getattr(pref, "repo", ""),
                "workdir": getattr(pref, "workdir", ""),
                "branch": getattr(pref, "branch", ""),
            }

        grouped: dict[str, list[HarnessRun]] = {}
        for run in self.list_runs():
            agent = str(run.source.get("agent") or run.source.get("profile") or run.source.get("user") or "Unassigned")
            grouped.setdefault(agent, []).append(run)

        agents = []
        for agent, runs in grouped.items():
            threads = []
            for run in sorted(runs, key=lambda r: r.updated_at or r.created_at, reverse=True):
                latest_time = run.updated_at or run.created_at
                threads.append({
                    "run_id": run.run_id,
                    "task_id": run.task_id,
                    "thread_key": run.thread_key,
                    "harness": run.harness,
                    "model": run.model,
                    "mode": run.mode,
                    "state": run.state,
                    "latest_message_time": latest_time,
                    "updated_at": run.updated_at,
                    "created_at": run.created_at,
                    "about": (run.goal or "").strip() or "Harness run",
                    "links": run.links,
                    "native": run.native,
                    "repo": run.repo,
                    "workdir": run.workdir,
                    "branch": run.branch,
                })
            agents.append({
                "agent": agent,
                "default_harness": _default_harness_for(agent),
                "threads": threads,
                "latest_message_time": threads[0]["latest_message_time"] if threads else "",
            })
        agents.sort(key=lambda a: (a.get("latest_message_time") or ""), reverse=True)
        return {"agents": agents}

    def append_event(self, run_id: str, event_type: str, payload: dict[str, Any] | None = None) -> None:
        d = self.run_dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": _now(), "run_id": run_id, "type": event_type, "payload": payload or {}}, sort_keys=True)
        with (d / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def append_transcript(self, run_id: str, text: str, *, stream: str = "stdout") -> None:
        run = self.load(run_id)
        path = Path(run.native.get("transcript_path") or self.run_dir(run_id) / "transcript.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        for line in (text or "").splitlines() or [""]:
            record = {"ts": _now(), "run_id": run_id, "stream": stream, "text": line}
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, sort_keys=True) + "\n")

    def record_result(self, run_id: str, *, exit_code: int, output: str) -> HarnessRun:
        run = self.load(run_id)
        self.append_transcript(run_id, output)
        session_id = detect_native_session_id(run.harness, output)
        if session_id:
            run.native["session_id"] = session_id
            run.commands["resume"] = resume_command(run.harness, session_id, run.workdir)
            run.commands["fork"] = fork_command(run.harness, session_id, run.workdir)
            self.append_event(run_id, "native_session_detected", {"session_id": session_id})
        run.exit_code = exit_code
        run.state = "completed" if exit_code == 0 else "failed"
        self.append_event(run_id, run.state, {"exit_code": exit_code})
        self.save(run)
        return run
