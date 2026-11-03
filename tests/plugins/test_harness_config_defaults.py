from __future__ import annotations


def test_auto_route_uses_effective_profile_config(tmp_path, monkeypatch):
    import plugins.harness_controller as hc

    home = tmp_path / "profile"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("TEST_HARNESS_PLATFORM", "slack")
    (home / "config.yaml").write_text(
        'harness:\n  auto_route:\n    enabled: false\n    platforms: ["${TEST_HARNESS_PLATFORM}"]\n',
        encoding="utf-8",
    )
    (managed / "config.yaml").write_text(
        "harness:\n  auto_route:\n    enabled: true\n", encoding="utf-8",
    )
    before = (home / "config.yaml").read_bytes()

    assert hc._auto_route_settings() == {"enabled": True, "platforms": ["slack"]}
    assert (home / "config.yaml").read_bytes() == before


def test_harness_preference_store_falls_back_to_profile_config(tmp_path, monkeypatch):
    from plugins.harness_controller.preferences import HarnessPreferenceStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
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


def test_harness_preferences_use_effective_profile_config(tmp_path, monkeypatch):
    from plugins.harness_controller.preferences import HarnessPreferenceStore

    home = tmp_path / "profile"
    managed = tmp_path / "managed"
    home.mkdir()
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    monkeypatch.setenv("TEST_HARNESS_MODEL", "managed-model")
    (home / "config.yaml").write_text(
        'harness:\n  default:\n    harness: codex\n    model: "${TEST_HARNESS_MODEL}"\n    mode: plan\n',
        encoding="utf-8",
    )
    (managed / "config.yaml").write_text(
        "harness:\n  default:\n    mode: auto\n", encoding="utf-8",
    )
    before = (home / "config.yaml").read_bytes()
    pref = HarnessPreferenceStore(home / "harness_config").get()

    assert pref.harness == "codex"
    assert pref.model == "managed-model"
    assert pref.mode == "auto"
    assert (home / "config.yaml").read_bytes() == before


def test_stored_harness_preference_overrides_profile_config(tmp_path, monkeypatch):
    from plugins.harness_controller.preferences import HarnessPreference, HarnessPreferenceStore

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
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
