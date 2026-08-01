from __future__ import annotations

from pathlib import Path

import yaml


def test_harness_controller_plugin_registers_command_and_slack_actions(tmp_path, monkeypatch):
    from hermes_cli.plugins import PluginManager, get_plugin_command_handler

    hermes_home = tmp_path / "hermes_home"
    plugin_dst = hermes_home / "plugins" / "harness_controller"
    plugin_src = Path(__file__).resolve().parents[2] / "plugins" / "harness_controller"
    plugin_dst.parent.mkdir(parents=True)
    import shutil
    shutil.copytree(plugin_src, plugin_dst)
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["harness_controller"]}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    import hermes_cli.plugins as plugins_mod
    plugins_mod._plugin_manager = mgr
    assert get_plugin_command_handler("harness") is not None
    action_ids = [entry[0] for entry in mgr.get_slack_action_handlers()]
    assert "harness_approve" in action_ids
    assert "harness_revise_plan" in action_ids
    assert "harness_cancel" in action_ids
