"""Read-only question-bank snapshots; never invoke skill setup or maintenance."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from contextlib import ExitStack, contextmanager
from pathlib import Path

import yaml


class QuestionBankError(ValueError):
    """A configured bank is unusable; callers must refuse interview entry."""


class _BankLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        depth = getattr(self, "_bank_depth", 0)
        if depth >= 20 or self.check_event(yaml.AliasEvent):
            raise QuestionBankError("Invalid question bank: aliases or excessive YAML nesting")
        self._bank_depth = depth + 1
        try:
            return super().compose_node(parent, index)
        finally:
            self._bank_depth = depth

    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise QuestionBankError("Invalid question bank: non-string or duplicate YAML key")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _questions(raw, seen):
    try:
        document = yaml.load(raw, Loader=_BankLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise QuestionBankError("Invalid question bank: unsafe or malformed YAML") from exc
    if not isinstance(document, dict) or set(document) != {"questions"} or not isinstance(document.get("questions"), list) or not document["questions"]:
        raise QuestionBankError("Invalid question bank: expected a nonempty questions list")
    for item in document["questions"]:
        if not isinstance(item, dict) or not all(_text(item.get(key)) for key in (
            "id", "category", "priority", "ask_when", "skip_when", "question", "resolves"
        )):
            raise QuestionBankError("Invalid question bank: missing/non-text required question fields")
        if set(item) - {"id", "category", "priority", "ask_when", "skip_when", "question", "resolves", "choices", "examples"}:
            raise QuestionBankError("Invalid question bank: unknown question fields")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", item["id"]) or item["id"] in seen:
            raise QuestionBankError("Invalid question bank: invalid or duplicate question id")
        seen.add(item["id"])
        if item["priority"] not in ("high", "medium", "low"):
            raise QuestionBankError("Invalid question bank: priority must be high, medium, or low")
        choices = item.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 4 or not all(_text(c) for c in choices):
            raise QuestionBankError("Invalid question bank: choices must contain at most four strings")
        examples = item.get("examples")
        if not isinstance(examples, list) or not examples or not all(
            isinstance(example, dict) and set(example) == {"task", "good", "bad"}
            and all(_text(example.get(key)) for key in ("task", "good", "bad"))
            for example in examples
        ):
            raise QuestionBankError("Invalid question bank: examples need task, good, and bad text")
    return document["questions"]


MAX_FILE_BYTES = 100_000
MAX_TOTAL_BYTES = 500_000
MAX_QUESTION_FILES = 16
MAX_REFERENCE_ENTRIES = 512


def _read(name, dir_fd):
    """Bound before decoding; reject links/devices/FIFOs, including at open."""
    try:
        if not stat.S_ISREG(os.stat(name, dir_fd=dir_fd, follow_symlinks=False).st_mode):
            raise QuestionBankError("Invalid question bank: expected a regular file")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(name, flags, dir_fd=dir_fd), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise QuestionBankError("Invalid question bank: expected a regular file")
            raw = stream.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise QuestionBankError("Invalid question bank: file exceeds byte limit")
        text = raw.decode("utf-8")
        if not text.strip():
            raise QuestionBankError("Invalid question bank: empty required file")
        return text, len(raw)
    except (OSError, UnicodeError) as exc:
        raise QuestionBankError(f"Invalid question bank: cannot read {name} as UTF-8") from exc


def load_question_bank(home=None) -> dict:
    """Load a pinned snapshot, preferring an explicit home over the runtime home.

    Only an absent custom directory selects the shipped bank. Invalid custom
    content raises QuestionBankError and never falls back. No caches, setup,
    installation, learning, or writes occur here. Persist the returned snapshot
    at interview entry; do not reload it between turns of an active interview.
    """
    from hermes_constants import get_hermes_home

    root = Path(home if home is not None else get_hermes_home()) / "skills" / "task-interview"
    try:
        with ExitStack() as stack:
            try:
                root_fd = stack.enter_context(_directory_path(root))
            except FileNotFoundError:
                root = Path(__file__).resolve().parents[1] / "skills/software-development/task-interview"
                root_fd = stack.enter_context(_directory_path(root))
            refs_fd = stack.enter_context(_directory("references", root_fd))
            return _load_snapshot(root_fd, refs_fd)
    except OSError as exc:
        raise QuestionBankError("Invalid question bank: cannot open bank/reference directories") from exc


@contextmanager
def _directory_path(path):
    """Walk from / without following user-controlled ancestor symlinks.

    macOS's fixed system aliases are expanded lexically, not by resolving the
    supplied path (which would also accept arbitrary custom bank symlinks).
    """
    parts = path.absolute().parts[1:]
    if sys.platform == "darwin" and parts and parts[0] in {"tmp", "var", "etc"}:
        parts = ("private", *parts)
    with ExitStack() as stack:
        fd = stack.enter_context(_directory("/"))
        for part in parts:
            fd = stack.enter_context(_directory(part, fd))
        yield fd


@contextmanager
def _directory(name, dir_fd=None):
    """Pin an actual directory; open its children relative to the descriptor."""
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    try:
        yield fd
    finally:
        os.close(fd)


def _load_snapshot(root_fd, refs_fd):
    paths = []
    with os.scandir(refs_fd) as entries:
        for count, entry in enumerate(entries, 1):
            if count > MAX_REFERENCE_ENTRIES:
                raise QuestionBankError("Invalid question bank: too many reference entries")
            if entry.name.endswith("-questions.yaml"):
                paths.append(entry.name)
                if len(paths) > MAX_QUESTION_FILES:
                    raise QuestionBankError("Invalid question bank: too many question files")
    if not paths:
        raise QuestionBankError("Invalid question bank: no question files")
    guidance, size = _read("SKILL.md", root_fd)
    frontmatter = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n(.*)\Z", guidance, re.DOTALL)
    if not frontmatter or not frontmatter[2].strip():
        raise QuestionBankError("Invalid question bank: SKILL.md needs frontmatter and guidance")
    try:
        metadata = yaml.load(frontmatter[1], Loader=_BankLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        raise QuestionBankError("Invalid question bank: malformed skill frontmatter") from exc
    if (not isinstance(metadata, dict) or metadata.get("name") != "task-interview"
            or not _text(metadata.get("description")) or len(metadata["description"]) > 1024):
        raise QuestionBankError("Invalid question bank: skill needs task-interview name and description")
    examples, example_size = _read("good-and-bad-examples.md", refs_fd)
    size += example_size
    questions = []
    seen = set()
    for path in sorted(paths):
        raw, length = _read(path, refs_fd)
        size += length
        if size > MAX_TOTAL_BYTES:
            raise QuestionBankError("Invalid question bank: total byte limit exceeded")
        questions.extend(_questions(raw, seen))
    prompt = guidance + "\n\n" + examples + "\n\n## Validated questions\n" + json.dumps(
        questions, ensure_ascii=False, sort_keys=True, indent=2
    )
    return {"version": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "prompt": prompt, "questions": questions}
