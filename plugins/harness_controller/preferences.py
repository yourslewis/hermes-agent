from __future__ import annotations

import json
import shlex
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .harnesses import normalize_mode


@dataclass(frozen=True)
class HarnessPreference:
    harness: str = "copilot"
    model: str = "gpt-5.4"
    mode: str = "plan"
    repo: str = ""
    workdir: str = ""
    branch: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "HarnessPreference":
        data = data or {}
        return cls(
            harness=str(data.get("harness") or "copilot"),
            model=str(data.get("model") or "gpt-5.4"),
            mode=normalize_mode(str(data.get("mode") or "plan")),
            repo=str(data.get("repo") or ""),
            workdir=str(data.get("workdir") or ""),
            branch=str(data.get("branch") or ""),
        )


@dataclass(frozen=True)
class HarnessRunArgs:
    harness: str
    model: str
    mode: str
    goal: str
    workdir: str = ""
    repo: str = ""
    branch: str = ""


class HarnessPreferenceStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.path = self.root / "preferences.json"
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _config_default(self, key: str = "default") -> dict[str, Any]:
        config_path = self.root.parent / "config.yaml"
        try:
            import yaml
            data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
        harness_cfg = (data.get("harness") or {}) if isinstance(data, dict) else {}
        if not isinstance(harness_cfg, dict):
            return {}
        default = harness_cfg.get(key)
        if default is None and key == "default":
            default = harness_cfg.get("default")
        return default if isinstance(default, dict) else {}

    def _write(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")

    def get(self, key: str = "default") -> HarnessPreference:
        stored = self._read().get(key)
        if stored:
            return HarnessPreference.from_dict(stored)
        return HarnessPreference.from_dict(self._config_default(key))

    def set(self, key: str, pref: HarnessPreference) -> None:
        data = self._read()
        data[key] = pref.to_dict()
        self._write(data)

    def clear(self, key: str | None = None) -> None:
        if key is None:
            self._write({})
            return
        data = self._read()
        data.pop(key, None)
        self._write(data)


def parse_preference_args(raw_args: str, current: HarnessPreference | None = None) -> HarnessPreference:
    tokens = shlex.split(raw_args or "")
    if tokens and tokens[0] == "set":
        tokens = tokens[1:]
    base = current or HarnessPreference()
    values = base.to_dict()
    positional: list[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in {"--harness", "-h"} and i + 1 < len(tokens):
            values["harness"] = tokens[i + 1]; i += 2
        elif token in {"--model", "-m"} and i + 1 < len(tokens):
            values["model"] = tokens[i + 1]; i += 2
        elif token == "--mode" and i + 1 < len(tokens):
            values["mode"] = normalize_mode(tokens[i + 1]); i += 2
        elif token == "--repo" and i + 1 < len(tokens):
            values["repo"] = tokens[i + 1]; i += 2
        elif token in {"--workdir", "--cwd", "--folder"} and i + 1 < len(tokens):
            values["workdir"] = tokens[i + 1]; i += 2
        elif token == "--branch" and i + 1 < len(tokens):
            values["branch"] = tokens[i + 1]; i += 2
        else:
            positional.append(token); i += 1
    if positional:
        values["harness"] = positional[0]
    if len(positional) > 1:
        values["model"] = positional[1]
    if len(positional) > 2:
        values["mode"] = normalize_mode(positional[2])
    values["mode"] = normalize_mode(values.get("mode"))
    return HarnessPreference.from_dict(values)


def preference_summary(pref: HarnessPreference) -> str:
    parts = [
        f"harness={pref.harness}",
        f"model={pref.model}",
        f"mode={pref.mode}",
    ]
    if pref.repo:
        parts.append(f"repo={pref.repo}")
    if pref.workdir:
        parts.append(f"workdir={pref.workdir}")
    if pref.branch:
        parts.append(f"branch={pref.branch}")
    return ", ".join(parts)
