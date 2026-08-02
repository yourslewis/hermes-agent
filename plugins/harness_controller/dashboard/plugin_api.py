from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter

from hermes_constants import get_hermes_home
from plugins.harness_controller.preferences import HarnessPreferenceStore
from plugins.harness_controller.run_registry import HarnessRunStore

router = APIRouter()


def _stores() -> tuple[HarnessRunStore, HarnessPreferenceStore]:
    home = Path(get_hermes_home())
    return HarnessRunStore(home / "harness_runs"), HarnessPreferenceStore(home / "harness_config")


@router.get("/overview")
def overview() -> dict[str, Any]:
    run_store, pref_store = _stores()
    return run_store.list_grouped_by_agent(pref_store)
