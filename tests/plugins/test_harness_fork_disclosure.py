"""Tests for the summary/answer fork-session disclosure.

``!hsummary`` / ``!hanswer`` deliberately run in a FRESH codex session rather
than resuming the run's native session: they are read-only meta-questions and
appending them would pollute the working session's context. The trade-off is
that the reply comes from a session the user cannot see in Canvas, so the id
must be disclosed. These tests pin that contract.
"""

from __future__ import annotations

import importlib
import inspect


def _load_module():
    return importlib.import_module("plugins.harness_controller")


class _Run:
    native: object

    def __init__(self, harness="codex", session_id=""):
        self.harness = harness
        self.run_id = "hrun_test"
        self.native = {"session_id": session_id}


def test_fork_disclosure_names_session_and_resume_command():
    mod = _load_module()
    line = mod._fork_disclosure_line(_Run(session_id="01a00ea8-3eb3-7f70-b866-c91d5eaf8967"))
    assert "01a00ea8-3eb3-7f70-b866-c91d5eaf8967" in line
    # The user must be able to act on it, not just see an opaque id.
    assert "codex resume 01a00ea8-3eb3-7f70-b866-c91d5eaf8967" in line
    # And must understand why it isn't in the working session.
    assert "isolated" in line.lower()


def test_fork_disclosure_empty_when_session_id_missing():
    """A run that never printed an id must degrade silently, not emit a stub."""
    mod = _load_module()
    assert mod._fork_disclosure_line(_Run(session_id="")) == ""


def test_fork_disclosure_empty_when_native_absent():
    mod = _load_module()
    run = _Run()
    run.native = None
    assert mod._fork_disclosure_line(run) == ""


def test_fork_disclosure_survives_non_dict_native():
    """Defensive: a malformed run must not break the Slack reply path."""
    mod = _load_module()
    run = _Run()
    run.native = ["unexpected"]
    assert mod._fork_disclosure_line(run) == ""


def test_summary_and_answer_do_not_resume_native_session():
    """Regression guard for the isolation decision itself.

    If someone later adds ``session_id=`` to these two call sites, summaries
    would start writing into the user's working session. That is a deliberate
    design reversal and should require updating this test, not slip through.
    """
    mod = _load_module()
    for fn_name in ("_handle_summary_event", "_handle_run_question_event"):
        src = inspect.getsource(getattr(mod, fn_name))
        _, _, after = src.partition("build_harness_command(")
        assert after, f"{fn_name} no longer builds a harness command"
        assert "session_id" not in after.split(")", 1)[0], (
            f"{fn_name} now resumes the native session; summaries/answers would "
            "pollute the working session. Update this test only if that is intended."
        )
        assert "_fork_disclosure_line" in src, f"{fn_name} does not disclose its fork session"
