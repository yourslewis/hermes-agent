from __future__ import annotations

import json


def test_harness_config_tool_sets_and_shows_repo_workdir(tmp_path, monkeypatch):
    import plugins.harness_controller as hc
    from plugins.harness_controller.preferences import HarnessPreferenceStore

    monkeypatch.setattr(hc, "_pref_store", HarnessPreferenceStore(tmp_path / "cfg"))
    monkeypatch.setattr(hc, "_preferences", {})

    set_result = json.loads(hc._tool_harness_config({
        "action": "set",
        "harness": "claude-code",
        "model": "gpt-5.5",
        "mode": "plan",
        "repo": "git@github.com:org/repo.git",
        "workdir": "/repo",
        "branch": "main",
    }))
    show_result = json.loads(hc._tool_harness_config({"action": "show"}))

    assert set_result["success"] is True
    assert set_result["preference"]["repo"] == "git@github.com:org/repo.git"
    assert show_result["preference"]["workdir"] == "/repo"
    assert show_result["preference"]["branch"] == "main"


def test_harness_run_tool_creates_run_record_with_canvas_url_without_executing(tmp_path, monkeypatch):
    import plugins.harness_controller as hc
    from plugins.harness_controller.preferences import HarnessPreference, HarnessPreferenceStore
    from plugins.harness_controller.run_registry import HarnessRunStore

    pref_store = HarnessPreferenceStore(tmp_path / "cfg")
    pref_store.set("default", HarnessPreference(
        harness="opencode",
        model="litellm/gpt-5.5",
        mode="plan",
        repo="https://github.com/org/repo",
        workdir="/repo",
        branch="main",
    ))
    monkeypatch.setattr(hc, "_pref_store", pref_store)
    monkeypatch.setattr(hc, "_run_store", HarnessRunStore(tmp_path / "runs"))
    monkeypatch.setattr(hc, "_preferences", {})

    result = json.loads(hc._tool_harness_run({"goal": "Smoke plan", "dry_run": True}))

    assert result["success"] is True
    assert result["run"]["harness"] == "opencode"
    assert result["run"]["repo"] == "https://github.com/org/repo"
    assert result["run"]["workdir"] == "/repo"
    assert result["run"]["branch"] == "main"
    assert result["run"]["links"]["canvas"].startswith("https://canvas.wenhao.dev/harness?run=hrun_")
    assert "opencode" in result["command"][0]


def test_harness_controller_registers_harness_toolset_tools(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager
    from model_tools import get_tool_definitions
    from tools.registry import registry
    import shutil
    from pathlib import Path
    import yaml

    hermes_home = tmp_path / "home"
    plugin_src = Path(__file__).resolve().parents[2] / "plugins" / "harness_controller"
    plugin_dst = hermes_home / "plugins" / "harness_controller"
    plugin_dst.parent.mkdir(parents=True)
    shutil.copytree(plugin_src, plugin_dst)
    (hermes_home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": ["harness_controller"]}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    before = set(registry.get_all_tool_names())
    mgr = PluginManager()
    mgr.discover_and_load()
    try:
        tools = get_tool_definitions(enabled_toolsets=["harness"], quiet_mode=True, skip_tool_search_assembly=True)
        names = {tool["function"]["name"] for tool in tools}
        assert {"harness_config", "harness_run"}.issubset(names)
    finally:
        for name in set(registry.get_all_tool_names()) - before:
            registry.deregister(name)
