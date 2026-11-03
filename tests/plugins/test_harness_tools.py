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
    assert result["run"]["links"]["remote_cli"] == "https://canvas.wenhao.dev/ui/opencode-cli/"
    assert result["run"]["links"]["vscode"].startswith("https://canvas.wenhao.dev/ui/vscode/")
    assert "opencode" in result["command"][0]


def test_harness_run_tool_with_hermes_default_does_not_spawn_external_command(tmp_path, monkeypatch):
    import plugins.harness_controller as hc
    from plugins.harness_controller.preferences import HarnessPreference, HarnessPreferenceStore
    from plugins.harness_controller.run_registry import HarnessRunStore

    pref_store = HarnessPreferenceStore(tmp_path / "cfg")
    pref_store.set("default", HarnessPreference(harness="hermes", model="gpt-5.6-sol", mode="plan"))
    monkeypatch.setattr(hc, "_pref_store", pref_store)
    monkeypatch.setattr(hc, "_run_store", HarnessRunStore(tmp_path / "runs"))
    monkeypatch.setattr(hc, "_preferences", {})

    result = json.loads(hc._tool_harness_run({"goal": "Smoke plan"}))

    assert result["success"] is True
    assert result["run"]["harness"] == "hermes"
    assert result["command"] == []
    assert "No external worker command" in result["output"]


def test_harness_run_child_has_closed_stdin_and_utf8_output(tmp_path, monkeypatch):
    import subprocess
    import sys
    from types import SimpleNamespace

    import plugins.harness_controller as hc
    from plugins.harness_controller.preferences import HarnessPreferenceStore
    from plugins.harness_controller.run_registry import HarnessRunStore

    monkeypatch.setattr(hc, "_pref_store", HarnessPreferenceStore(tmp_path / "cfg"))
    monkeypatch.setattr(hc, "_run_store", HarnessRunStore(tmp_path / "runs"))
    argv = [sys.executable, "-c",
            "import sys; assert sys.stdin.read() == ''; "
            "sys.stdout.buffer.write('你好'.encode('utf-8') + b'\\xff')"]
    monkeypatch.setattr(hc, "build_harness_command", lambda **kw: SimpleNamespace(
        argv=argv, cwd=str(tmp_path), harness="codex"))
    real_run = subprocess.run

    def run_child(*args, **kwargs):
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("errors") == "replace"
        return real_run(*args, **kwargs)

    monkeypatch.setattr(hc.subprocess, "run", run_child)
    result = json.loads(hc._tool_harness_run({"goal": "Read fixture", "timeout": 10}))

    assert result["success"] is True
    assert result["output"] == "你好\ufffd"
    persisted = hc._run_store.load(result["run"]["run_id"])
    assert persisted.state == "completed"
    transcript = hc._run_store.run_dir(persisted.run_id) / "transcript.jsonl"
    assert json.loads(transcript.read_text(encoding="utf-8"))["text"] == result["output"]


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
