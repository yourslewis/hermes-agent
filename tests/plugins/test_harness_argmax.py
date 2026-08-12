from __future__ import annotations

import asyncio

import pytest

# Regression coverage for the ARG_MAX incident: a ~2.4MB auto_prompt was passed
# to `codex` as a single argv entry, exec failed with OSError(E2BIG) "Argument
# list too long", and the run was left stranded in state=created with no native
# session id — which surfaced to the user as "native resume unavailable".

_HUGE = "x" * 200_000
_SMALL = "do the thing"


def _spec(harness: str, prompt: str):
    from plugins.harness_controller.harnesses import build_harness_command

    return build_harness_command(
        harness=harness, model="test-model", mode="auto", prompt=prompt, workdir=None
    )


def _argv_bytes(spec) -> int:
    return sum(len(a.encode()) for a in spec.argv)


@pytest.mark.parametrize("harness", ["codex", "claude", "copilot"])
def test_oversized_prompt_never_reaches_argv(harness):
    from plugins.harness_controller.harnesses import MAX_ARGV_PROMPT_BYTES

    spec = _spec(harness, _HUGE)
    assert _argv_bytes(spec) <= MAX_ARGV_PROMPT_BYTES
    assert _HUGE not in spec.argv
    # The full prompt is always preserved for the caller to deliver.
    assert _HUGE in spec.prompt


def test_codex_oversized_prompt_reads_stdin():
    spec = _spec("codex", _HUGE)
    assert spec.stdin_prompt is True
    assert spec.argv[-1] == "-"


def test_claude_oversized_prompt_reads_stdin():
    spec = _spec("claude", _HUGE)
    assert spec.stdin_prompt is True
    # Bare -p (no inline prompt) is what makes claude read stdin.
    assert "-p" in spec.argv
    assert spec.argv[spec.argv.index("-p") + 1].startswith("--")


def test_copilot_oversized_prompt_spills_to_file():
    # Copilot's -p has no stdin mode, so the prompt goes to a file instead.
    spec = _spec("copilot", _HUGE)
    assert spec.stdin_prompt is False
    inline = spec.argv[spec.argv.index("-p") + 1]
    path = inline.splitlines()[-1]
    assert open(path, encoding="utf-8").read() == spec.prompt


@pytest.mark.parametrize("harness", ["codex", "claude", "copilot"])
def test_small_prompt_still_passed_inline(harness):
    spec = _spec(harness, _SMALL)
    assert spec.stdin_prompt is False
    assert any(_SMALL in a for a in spec.argv)


def test_run_capture_reports_spawn_failure_instead_of_raising():
    from plugins.harness_controller import _run_capture

    # A missing binary raises OSError from create_subprocess_exec exactly like
    # E2BIG did. It must degrade to a failed run, not escape and kill the
    # platform action handler.
    code, output = asyncio.run(_run_capture(["definitely-not-a-real-binary-xyz"]))
    assert code == 126
    assert "Failed to start harness process" in output


def test_run_capture_feeds_stdin_to_child():
    from plugins.harness_controller import _run_capture

    code, output = asyncio.run(_run_capture(["cat"], stdin_text="piped-payload"))
    assert code == 0
    assert "piped-payload" in output
