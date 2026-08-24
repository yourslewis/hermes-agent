from __future__ import annotations


def test_hermes_harness_command_is_noop_and_records_harness_name():
    from plugins.harness_controller.harnesses import build_harness_command

    spec = build_harness_command(
        harness="hermes",
        model="gpt-5.6-sol",
        mode="plan",
        prompt="Plan this coding task",
        workdir="/repo",
    )

    assert spec.harness == "hermes"
    assert spec.argv == []
    assert spec.cwd == "/repo"
    assert "PLAN mode" in spec.prompt
