from __future__ import annotations


def test_harness_preference_store_falls_back_to_profile_config(tmp_path):
    from plugins.harness_controller.preferences import HarnessPreferenceStore

    (tmp_path / "config.yaml").write_text(
        """
harness:
  default:
    harness: codex
    model: gpt-5.6-sol
    mode: plan
    repo: git@github.com:org/repo.git
    workdir: /Users/me/repo
    branch: main
""".strip(),
        encoding="utf-8",
    )

    pref = HarnessPreferenceStore(tmp_path / "harness_config").get("default")

    assert pref.harness == "codex"
    assert pref.model == "gpt-5.6-sol"
    assert pref.mode == "plan"
    assert pref.repo == "git@github.com:org/repo.git"
    assert pref.workdir == "/Users/me/repo"
    assert pref.branch == "main"


def test_stored_harness_preference_overrides_profile_config(tmp_path):
    from plugins.harness_controller.preferences import HarnessPreference, HarnessPreferenceStore

    (tmp_path / "config.yaml").write_text(
        """
harness:
  default:
    harness: codex
    model: gpt-5.6-sol
    mode: plan
""".strip(),
        encoding="utf-8",
    )
    store = HarnessPreferenceStore(tmp_path / "harness_config")
    store.set("default", HarnessPreference(harness="opencode", model="litellm/gpt-5.5", mode="auto"))

    pref = store.get("default")

    assert pref.harness == "opencode"
    assert pref.model == "litellm/gpt-5.5"
    assert pref.mode == "auto"
