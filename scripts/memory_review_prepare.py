#!/usr/bin/env python3
"""Read-only memory review preparation for the active HERMES_HOME.

No profile enumeration, configuration changes, Slack calls, or gateway restart.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.memory_review import preparation_report

if __name__ == "__main__":
    print(json.dumps(preparation_report(), indent=2))
