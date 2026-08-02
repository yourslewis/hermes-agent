from __future__ import annotations

from .controller import HarnessTask


def build_plan_blocks(task: HarnessTask) -> list[dict]:
    plan = task.plan_text or "No plan text captured."
    if len(plan) > 2600:
        plan = plan[:2590] + "…"
    canvas = getattr(task, "canvas_url", "") or ""
    remote_cli = getattr(task, "remote_cli_url", "") or ""
    vscode = getattr(task, "vscode_url", "") or ""
    link_lines = []
    if canvas:
        link_lines.append(f"*Open in Canvas:* <{canvas}|{canvas}>")
    if remote_cli:
        link_lines.append(f"*Remote CLI:* <{remote_cli}|{remote_cli}>")
    if vscode:
        link_lines.append(f"*VS Code:* <{vscode}|{vscode}>")
    canvas_line = "\n" + "\n".join(link_lines) + "\n" if link_lines else ""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Harness plan ready*\n"
                    f"*Harness:* `{task.harness}`\n"
                    f"*Model:* `{task.model}`\n"
                    f"*Task:* {task.goal}\n"
                    f"{canvas_line}\n"
                    f"{plan}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": task.approve_label, "emoji": True},
                    "style": "primary",
                    "action_id": "harness_approve",
                    "value": task.task_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": task.revise_label, "emoji": True},
                    "action_id": "harness_revise_plan",
                    "value": task.task_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                    "style": "danger",
                    "action_id": "harness_cancel",
                    "value": task.task_id,
                },
            ],
        },
    ]
