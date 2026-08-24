from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class ParsedQuestion:
    question_id: str
    question: str
    choices: list[dict[str, str]] = field(default_factory=list)
    allow_freeform: bool = False
    why_needed: str = ""


@dataclass
class ParsedHarnessOutput:
    summary: str = ""
    evidence: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    open_questions: list[ParsedQuestion] = field(default_factory=list)
    can_stop: str = ""


_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _sections(text: str) -> dict[str, str]:
    matches = list(_HEADING_RE.finditer(text or ""))
    out: dict[str, str] = {}
    for idx, match in enumerate(matches):
        name = match.group(1).strip().upper()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        out[name] = text[start:end].strip()
    return out


def _parse_choices(block: str) -> list[dict[str, str]]:
    choices: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in block.splitlines():
        line = raw.strip()
        m = re.match(r"[-*]\s+id:\s*(.+)$", line)
        if m:
            if current:
                choices.append(current)
            current = {"id": m.group(1).strip()}
            continue
        if current:
            m = re.match(r"(?:[-*]\s+)?label:\s*(.+)$", line)
            if m:
                current["label"] = m.group(1).strip()
                continue
            m = re.match(r"(?:[-*]\s+)?consequence:\s*(.+)$", line)
            if m:
                current["consequence"] = m.group(1).strip()
                continue
        m = re.match(r"(?:[-*]|\d+[.)])\s+(.+)$", line)
        if m and not line.lower().startswith(('- id:', '* id:')):
            label = m.group(1).strip()
            if label and ":" not in label[:16].lower():
                choices.append({"id": re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:32] or f"choice_{len(choices)+1}", "label": label})
    if current:
        choices.append(current)
    for idx, choice in enumerate(choices, start=1):
        choice.setdefault("id", f"choice_{idx}")
        choice.setdefault("label", choice["id"])
    # de-dupe preserving order
    seen = set()
    unique = []
    for choice in choices:
        key = choice.get("id") or choice.get("label")
        if key in seen:
            continue
        seen.add(key)
        unique.append(choice)
    return unique


def _is_no_open_questions(block: str) -> bool:
    """Return True when an OPEN QUESTIONS section explicitly says none remain."""
    compact = " ".join((block or "").strip().lower().split())
    if not compact:
        return True
    no_question_markers = [
        "none pending",
        "no open questions",
        "none remaining",
        "no pending questions",
        "nothing pending",
        "no clarification needed",
        "no further clarification",
    ]
    return any(marker in compact for marker in no_question_markers)


def parse_harness_output(text: str) -> ParsedHarnessOutput:
    sections = _sections(text or "")
    parsed = ParsedHarnessOutput(
        summary=sections.get("SUMMARY", "").strip(),
        can_stop=sections.get("CAN STOP", "").strip(),
    )
    for name, attr in [("EVIDENCE", "evidence"), ("ACTIONS", "actions")]:
        block = sections.get(name, "")
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        setattr(parsed, attr, lines)

    questions_block = sections.get("OPEN QUESTIONS", "")
    if questions_block:
        if _is_no_open_questions(questions_block):
            return parsed
        chunks = re.split(r"(?=^-\s*id:\s*)", questions_block, flags=re.MULTILINE)
        for idx, chunk in enumerate([c.strip() for c in chunks if c.strip()], start=1):
            if _is_no_open_questions(chunk):
                continue
            qid_match = re.search(r"(?:^|\n)-?\s*id:\s*(.+)", chunk)
            question_match = re.search(r"(?:^|\n)\s*question:\s*(.+)", chunk)
            allow_match = re.search(r"(?:^|\n)\s*allow_freeform:\s*(true|false|yes|no)", chunk, re.I)
            why_match = re.search(r"(?:^|\n)\s*why_needed:\s*(.+)", chunk)
            choices_match = re.search(r"(?:^|\n)\s*choices:\s*\n(?P<choices>.*?)(?=\n\s*(?:allow_freeform|why_needed|question|id):|\Z)", chunk, re.S)
            choices = _parse_choices(choices_match.group("choices") if choices_match else chunk)
            # Avoid inventing a new pending question from narrative prose. We
            # only create a fallback question when there are actual choices or
            # explicit freeform/other language.
            allow_freeform = (allow_match.group(1).lower() in {"true", "yes"} if allow_match else "freeform" in chunk.lower() or "other" in chunk.lower())
            if not question_match and not choices and not allow_freeform:
                continue
            parsed.open_questions.append(
                ParsedQuestion(
                    question_id=(qid_match.group(1).strip() if qid_match else f"q{idx:03d}"),
                    question=(question_match.group(1).strip() if question_match else "Harness needs clarification."),
                    choices=choices,
                    allow_freeform=allow_freeform,
                    why_needed=(why_match.group(1).strip() if why_match else ""),
                )
            )
    return parsed
