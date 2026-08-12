from __future__ import annotations

import json
import os


def _result_json(session_id: str, result: str = "done") -> str:
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": result,
            "session_id": session_id,
        }
    )


# --- build_harness_command -------------------------------------------------


def test_claude_resume_flag_precedes_prompt():
    from plugins.harness_controller.harnesses import build_harness_command

    spec = build_harness_command(
        harness="claude-code",
        model="sonnet",
        mode="ask",
        prompt="carry on",
        session_id="abc-123",
    )

    assert spec.argv[:3] == ["claude", "--resume", "abc-123"]
    # Resume must be parsed before -p, otherwise the id is read as the prompt.
    assert spec.argv.index("--resume") < spec.argv.index("-p")
    # Text output is required: parse_harness_output() reads prose sections.
    assert "--output-format" not in spec.argv


def test_claude_pins_new_session_id_when_not_resuming():
    """Pinning the id at creation beats scraping it back out of output."""
    from plugins.harness_controller.harnesses import build_harness_command

    spec = build_harness_command(
        harness="claude-code",
        model="sonnet",
        mode="ask",
        prompt="start",
        new_session_id="fresh-1",
    )

    assert spec.argv[:3] == ["claude", "--session-id", "fresh-1"]
    assert "--resume" not in spec.argv


def test_claude_resume_takes_precedence_over_new_session_id():
    from plugins.harness_controller.harnesses import build_harness_command

    spec = build_harness_command(
        harness="claude-code",
        model="sonnet",
        mode="ask",
        prompt="go",
        session_id="old-1",
        new_session_id="fresh-1",
    )

    assert "--resume" in spec.argv
    assert "--session-id" not in spec.argv


def test_claude_without_session_id_has_no_resume_flag():
    from plugins.harness_controller.harnesses import build_harness_command

    spec = build_harness_command(
        harness="claude-code", model="sonnet", mode="ask", prompt="hello"
    )

    assert "--resume" not in spec.argv


def test_claude_resume_survives_stdin_prompt_fallback():
    """A prompt too long for argv must not silently drop the resume flag."""
    from plugins.harness_controller.harnesses import build_harness_command

    spec = build_harness_command(
        harness="claude-code",
        model="sonnet",
        mode="ask",
        prompt="x" * 200_000,
        session_id="abc-123",
    )

    assert spec.stdin_prompt is True
    assert spec.argv[:3] == ["claude", "--resume", "abc-123"]


# --- classify_resume -------------------------------------------------------


def test_classify_resume_success():
    from plugins.harness_controller.run_registry import classify_resume

    verdict = classify_resume("codex", "sid-1", _result_json("sid-1"), 0)

    assert verdict == {
        "attempted": True,
        "resumed": True,
        "session_id": "sid-1",
        "reason": "ok",
    }


def test_classify_resume_detects_missing_session():
    """The exact CLI failure string, observed from claude 2.1.81."""
    from plugins.harness_controller.run_registry import classify_resume

    out = "No conversation found with session ID: sid-1"
    verdict = classify_resume("claude-code", "sid-1", out, 1)

    assert verdict["attempted"] is True
    assert verdict["resumed"] is False
    assert verdict["reason"] == "session_not_found"


def test_classify_resume_flags_session_id_mismatch():
    """A different id back means a fork/cold start -- no prior context."""
    from plugins.harness_controller.run_registry import classify_resume

    verdict = classify_resume("claude-code", "sid-1", _result_json("sid-2"), 0)

    assert verdict["resumed"] is False
    assert verdict["reason"] == "session_id_mismatch"
    assert verdict["session_id"] == "sid-2"


def test_classify_resume_requires_positive_evidence():
    """Silence is not success for harnesses that echo an id (codex)."""
    from plugins.harness_controller.run_registry import classify_resume

    verdict = classify_resume("codex", "sid-1", "some prose, no json", 0)

    assert verdict["resumed"] is False
    assert verdict["reason"] == "no_session_id_in_output"


def test_classify_resume_claude_text_mode_counts_as_resumed():
    """Claude runs in text mode and echoes no id; a clean exit with no
    "not found" message is the positive signal, since we supplied the id."""
    from plugins.harness_controller.run_registry import classify_resume

    verdict = classify_resume("claude-code", "sid-1", "SUMMARY\nDid the work.", 0)

    assert verdict["resumed"] is True
    assert verdict["session_id"] == "sid-1"
    assert verdict["reason"] == "ok"


def test_classify_resume_nonzero_exit_is_not_resumed():
    from plugins.harness_controller.run_registry import classify_resume

    verdict = classify_resume("claude-code", "sid-1", _result_json("sid-1"), 3)

    assert verdict["resumed"] is False
    assert verdict["reason"] == "nonzero_exit:3"


def test_classify_resume_no_request_is_not_an_attempt():
    from plugins.harness_controller.run_registry import classify_resume

    verdict = classify_resume("claude-code", None, _result_json("sid-9"), 0)

    assert verdict["attempted"] is False
    assert verdict["resumed"] is False
    # Still harvests the id so the next turn can resume it.
    assert verdict["session_id"] == "sid-9"


# --- find_claude_session ---------------------------------------------------


def test_find_claude_session_is_scoped_to_workdir(tmp_path):
    """Claude partitions sessions per project dir; a hit in the wrong dir is
    not resumable, which is exactly the silent-cold-start trap."""
    from plugins.harness_controller.native_transcript import (
        claude_project_dir_name,
        find_claude_session,
    )

    work = tmp_path / "work"
    work.mkdir()
    root = tmp_path / "projects"
    proj = root / claude_project_dir_name(work)
    proj.mkdir(parents=True)
    (proj / "sid-1.jsonl").write_text("{}", encoding="utf-8")

    assert find_claude_session("sid-1", work, root=root) is not None
    assert find_claude_session("missing", work, root=root) is None
    assert find_claude_session("", work, root=root) is None


def test_find_claude_session_rejects_other_project_dirs(tmp_path):
    """Regression: a session that exists under a DIFFERENT project dir is not
    resumable from here. An earlier glob fallback returned it, which would
    have green-lit the silent cold start this check exists to prevent
    (observed live: resume from the wrong cwd exits 1)."""
    from plugins.harness_controller.native_transcript import (
        claude_project_dir_name,
        find_claude_session,
    )

    work = tmp_path / "work"
    work.mkdir()
    other = tmp_path / "other"
    other.mkdir()
    root = tmp_path / "projects"
    proj = root / claude_project_dir_name(other)
    proj.mkdir(parents=True)
    (proj / "sid-1.jsonl").write_text("{}", encoding="utf-8")

    assert find_claude_session("sid-1", other, root=root) is not None
    assert find_claude_session("sid-1", work, root=root) is None


def test_claude_project_dir_name_folds_underscores(tmp_path):
    """Regression: claude folds BOTH '/' and '_' to '-'. Encoding only '/'
    produced a path that never matched, so a resumable session read as
    missing (observed live with /Users/x/cc_rt/work -> -Users-x-cc-rt-work)."""
    from plugins.harness_controller.native_transcript import claude_project_dir_name

    work = tmp_path / "cc_rt" / "work"
    work.mkdir(parents=True)

    name = claude_project_dir_name(work)

    assert "_" not in name
    assert name.endswith("-cc-rt-work")


def test_claude_project_dir_name_resolves_symlinks(tmp_path):
    from plugins.harness_controller.native_transcript import claude_project_dir_name

    name = claude_project_dir_name(tmp_path)

    assert "/" not in name
    assert name.startswith("-")


# --- record_result wiring --------------------------------------------------


def test_effective_workdir_resolves_empty_to_real_cwd():
    """Regression: runs recorded workdir='' when none was requested.

    subprocess inherits the gateway's cwd in that case, and claude derives the
    session's project dir from it -- so '' threw away the one fact resume needs.
    Canvas then had to ask the user to pick a folder, and a wrong pick exits 1
    and reconnect-loops forever.
    """
    import os

    from plugins.harness_controller import _effective_workdir

    assert _effective_workdir(None) == os.getcwd()
    assert _effective_workdir("") == os.getcwd()
    assert os.path.isabs(_effective_workdir(None))


def test_effective_workdir_resolves_relative_and_symlinks(tmp_path):
    from plugins.harness_controller import _effective_workdir

    got = _effective_workdir(str(tmp_path))

    assert got == str(tmp_path.resolve())
    assert os.path.isabs(got)


def test_resume_command_includes_cd_once_workdir_is_recorded():
    """With a real workdir the resume command carries the cd that makes it
    work from any shell -- previously it was a bare `claude --resume <id>`
    that silently depended on the user's current directory."""
    from plugins.harness_controller.run_registry import resume_command

    cmd = resume_command("claude-code", "sid-1", "/Users/x/.hermes/profiles/rex")

    assert cmd == "cd /Users/x/.hermes/profiles/rex && claude --resume sid-1"


def test_record_result_keeps_pinned_session_id_on_timeout(tmp_path):
    """A timeout (124) must NOT discard the pinned id.

    This is the "native resume unavailable" bug: claude persists the session to
    disk as it works, so a run killed at 1200s leaves real work behind. The id
    is the only pointer to it, and a killed process never prints one -- which
    is exactly why it is pinned at launch instead of scraped from output.
    """
    from plugins.harness_controller.run_registry import HarnessRunStore

    store = HarnessRunStore(tmp_path)
    run = store.create_run(
        task_id="htask_abc",
        thread_key="slack:C1:123.4",
        harness="claude-code",
        model="sonnet",
        mode="auto",
        goal="Submit the AML job",
        workdir="/repo",
        source={},
        handoff={"pinned_session_id": "pinned-1"},
    )

    updated = store.record_result(
        run.run_id, exit_code=124, output="Harness command timed out."
    )

    assert updated.native["session_id"] == "pinned-1"
    assert updated.state == "failed"
    # and the resume affordances must be populated, not placeholders
    assert "pinned-1" in updated.commands["resume"]
    assert "session=pinned-1" in updated.links["resume_native"]


def test_record_result_persists_resume_verdict(tmp_path):
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
        source={},
        handoff={"resumed_session_id": "sid-1"},
    )

    updated = store.record_result(
        run.run_id, exit_code=1, output="No conversation found with session ID: sid-1"
    )

    assert updated.native["resume"]["resumed"] is False
    assert updated.native["resume"]["reason"] == "session_not_found"
