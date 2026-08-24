from __future__ import annotations

from .controller import HarnessTask


SLACK_SECTION_TEXT_LIMIT = 3000
PLAN_CHUNK_LIMIT = 2700
MAX_PLAN_SECTION_BLOCKS = 8


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _section(text: str) -> dict:
    return {
        "type": "section",
        "text": {
            "type": "mrkdwn",
            "text": _clip(text, SLACK_SECTION_TEXT_LIMIT),
        },
    }


def _chunk_text(text: str, limit: int) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def build_plan_blocks(task: HarnessTask) -> list[dict]:
    plan = task.plan_text or "No plan text captured."
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
    links = "\n".join(link_lines)

    header = (
        f"*Harness plan ready*\n"
        f"*Harness:* `{task.harness}`\n"
        f"*Model:* `{task.model}`\n"
        f"*Task:* {_clip(task.goal, 900)}"
    )
    if links:
        header = f"{header}\n{links}"

    blocks: list[dict] = [_section(header)]

    plan_chunks = _chunk_text(plan, PLAN_CHUNK_LIMIT)
    omitted = max(0, len(plan_chunks) - MAX_PLAN_SECTION_BLOCKS)
    visible_chunks = plan_chunks[:MAX_PLAN_SECTION_BLOCKS]
    for idx, chunk in enumerate(visible_chunks, start=1):
        title = "*Plan*" if len(plan_chunks) == 1 else f"*Plan ({idx}/{len(plan_chunks)})*"
        blocks.append(_section(f"{title}\n{chunk}"))
    if omitted:
        blocks.append(_section(f"_Plan truncated: {omitted} additional section(s) omitted. Open Canvas for the full run transcript._"))

    blocks.append(
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
        }
    )
    return blocks
