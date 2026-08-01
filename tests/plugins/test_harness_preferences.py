from __future__ import annotations


def test_parse_harness_preference_set_with_repo_workdir_and_branch():
    from plugins.harness_controller.preferences import parse_preference_args

    pref = parse_preference_args(
        "set --harness claude-code --model gpt-5.5 --mode plan "
        "--repo git@github.com:org/repo.git --workdir /Users/me/repo --branch main"
    )

    assert pref.harness == "claude-code"
    assert pref.model == "gpt-5.5"
    assert pref.mode == "plan"
    assert pref.repo == "git@github.com:org/repo.git"
    assert pref.workdir == "/Users/me/repo"
    assert pref.branch == "main"


def test_harness_preference_store_roundtrips_default(tmp_path):
    from plugins.harness_controller.preferences import HarnessPreference, HarnessPreferenceStore

    store = HarnessPreferenceStore(tmp_path)
    pref = HarnessPreference(
        harness="opencode",
        model="litellm/gpt-5.5",
        mode="ask",
        repo="https://github.com/org/repo",
        workdir="/repo",
        branch="dev",
    )

    store.set("default", pref)

    assert store.get("default") == pref
    assert HarnessPreferenceStore(tmp_path).get("default") == pref


def test_parse_run_args_inherits_harness_repo_workdir_preference(monkeypatch):
    import plugins.harness_controller as hc
    from plugins.harness_controller.preferences import HarnessPreference

    monkeypatch.setitem(
        hc._preferences,
        "default",
        HarnessPreference(
            harness="codex",
            model="gpt-5.6-sol",
            mode="auto",
            repo="git@github.com:org/repo.git",
            workdir="/Users/me/repo",
            branch="main",
        ).to_dict(),
    )

    parsed = hc._parse_run_args("Fix the bug")

    assert parsed.harness == "codex"
    assert parsed.model == "gpt-5.6-sol"
    assert parsed.mode == "auto"
    assert parsed.goal == "Fix the bug"
    assert parsed.repo == "git@github.com:org/repo.git"
    assert parsed.workdir == "/Users/me/repo"
    assert parsed.branch == "main"


def test_parse_run_args_allows_per_run_repo_workdir_override(monkeypatch):
    import plugins.harness_controller as hc

    monkeypatch.setitem(hc._preferences, "default", {})

    parsed = hc._parse_run_args(
        "--harness opencode --model litellm/gpt-5.5 --mode plan "
        "--repo https://github.com/other/repo --workdir /tmp/repo --branch feature Do work"
    )

    assert parsed.harness == "opencode"
    assert parsed.model == "litellm/gpt-5.5"
    assert parsed.mode == "plan"
    assert parsed.repo == "https://github.com/other/repo"
    assert parsed.workdir == "/tmp/repo"
    assert parsed.branch == "feature"
    assert parsed.goal == "Do work"
