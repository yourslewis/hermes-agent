from __future__ import annotations

from .controller import HarnessTask
from .store import latest_open_question


def build_clarification_blocks(task: HarnessTask) -> list[dict]:
    question = latest_open_question(task)
    if not question:
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"No pending clarification for `{task.task_id}`."}}
        ]
    text = f"*Harness needs clarification*\nTask: `{task.task_id}`\n\n*Question:*\n{question.get('question', '')}"
    if question.get("why_needed"):
        text += f"\n\n_Why needed:_ {question['why_needed']}"
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]
    elements = []
    for idx, choice in enumerate((question.get("choices") or [])[:4]):
        label = str(choice.get("label") or choice.get("id") or "Choice")[:75]
        value = f"{task.task_id}|{question.get('id')}|{choice.get('id')}|{label}"
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": label},
                "style": "primary",
                # Slack requires action_id values to be unique within a single
                # actions block. Register a small fixed fan-out of handlers in
                # the plugin and route all of them to the same callback.
                "action_id": f"harness_answer_choice_{idx}",
                "value": value[:2000],
            }
        )
    if question.get("allow_freeform"):
        elements.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Other / freeform"},
                "action_id": "harness_answer_other",
                "value": f"{task.task_id}|{question.get('id')}"[:2000],
            }
        )
    elements.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": task.cancel_label, "emoji": True},
            "style": "danger",
            "action_id": "harness_cancel",
            "value": task.task_id,
        }
    )
    blocks.append({"type": "actions", "elements": elements[:5]})
    return blocks
