"""Question banks are explicit, read-only, reproducible interview snapshots."""
import hashlib
import json
import sys
from pathlib import Path

import pytest
import yaml


def question(**changes):
    item = {
        "id": "success", "category": "common", "priority": "high",
        "ask_when": "Success is unclear.", "skip_when": "Success is measurable already.",
        "question": "What result would count as success?", "choices": ["Report", "Prototype"],
        "resolves": "Acceptance criterion.",
        "examples": [{"task": "Improve a dashboard", "good": "Which comparison matters?",
                      "bad": "More details?"}],
    }
    return dict(item, **changes)


def custom_bank(home, items=None):
    root = home / "skills" / "task-interview"
    refs = root / "references"
    refs.mkdir(parents=True)
    (root / "SKILL.md").write_text("---\nname: task-interview\ndescription: Use when interviewing.\n---\n# Guidance\nAsk only unresolved questions.\n")
    (refs / "common-questions.yaml").write_text(yaml.safe_dump({"questions": items or [question()]}))
    (refs / "good-and-bad-examples.md").write_text("# Examples\nGOOD: a decision. BAD: vague detail requests.\n")
    return root


def test_custom_bank_is_full_stable_snapshot(tmp_path):
    from agent.interview_questions import load_question_bank

    root = custom_bank(tmp_path)
    before = {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    bank = load_question_bank(home=tmp_path)
    assert set(bank) == {"version", "prompt", "questions"}
    assert bank["questions"] == [question()]
    assert (root / "SKILL.md").read_text() in bank["prompt"]
    assert (root / "references/good-and-bad-examples.md").read_text() in bank["prompt"]
    assert json.dumps(bank["questions"], ensure_ascii=False, sort_keys=True, indent=2) in bank["prompt"]
    assert bank["version"] == hashlib.sha256(bank["prompt"].encode()).hexdigest()
    assert bank == load_question_bank(home=tmp_path)
    assert before == {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (root / "SKILL.md").write_text((root / "SKILL.md").read_text() + "Changed guidance.\n")
    updated = load_question_bank(home=tmp_path)
    assert updated["version"] != bank["version"]
    assert "Changed guidance" not in bank["prompt"]
    updated["questions"][0]["choices"].append("Mutation")
    assert "Mutation" not in load_question_bank(home=tmp_path)["questions"][0]["choices"]


@pytest.mark.parametrize("items", [
    [question(), question()],
    *[[{k: v for k, v in question().items() if k != missing}]
      for missing in ("id", "category", "priority", "ask_when", "skip_when", "question", "resolves", "examples")],
    [question(choices=["a", "b", "c", "d", "e"])],
    [question(choices="not a list")], [question(choices=[False])],
    [question(priority="urgent")], [question(category="")],
    [question(ask_when=None)], [question(id="bad id")],
    [question(examples=[])], [question(examples=[{"task": "x", "good": "y"}])],
    [question(examples=[{"task": "x", "good": "y", "bad": False}])],
    ["not a question"],
])
def test_invalid_custom_schema_fails_without_fallback(tmp_path, items):
    from agent.interview_questions import load_question_bank

    custom_bank(tmp_path, items)
    with pytest.raises(ValueError, match="question bank"):
        load_question_bank(tmp_path)


@pytest.mark.parametrize("raw", [
    "questions: []", "[]", "questions: nope", "[", "questions: !!python/object:os.system {}",
    "questions: []\nquestions: []", "questions: &loop [*loop]",
])
def test_invalid_yaml_is_rejected_safely(tmp_path, raw):
    from agent.interview_questions import load_question_bank

    root = custom_bank(tmp_path)
    (root / "references/common-questions.yaml").write_text(raw)
    with pytest.raises(ValueError, match="question bank"):
        load_question_bank(tmp_path)


@pytest.mark.parametrize("target", ["SKILL.md", "references/good-and-bad-examples.md", "references/common-questions.yaml"])
@pytest.mark.parametrize("damage", ["missing", "empty", "oversize", "symlink", "invalid_utf8"])
def test_required_files_are_bounded_regular_utf8(tmp_path, target, damage):
    from agent.interview_questions import load_question_bank

    root = custom_bank(tmp_path)
    path = root / target
    if damage == "missing":
        path.unlink()
    elif damage == "empty":
        path.write_text("")
    elif damage == "oversize":
        path.write_bytes(b"x" * 100_001)
    elif damage == "invalid_utf8":
        path.write_bytes(b"\xff")
    else:
        external = tmp_path / "external"
        external.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(external)
    with pytest.raises(ValueError, match="question bank"):
        load_question_bank(tmp_path)


def test_yaml_aliases_and_deep_nesting_are_rejected(tmp_path):
    from agent.interview_questions import load_question_bank

    root = custom_bank(tmp_path)
    path = root / "references/common-questions.yaml"
    for raw in ("unused: &x [1]\nquestions: *x", "questions: " + "[" * 100 + "]" * 100):
        path.write_text(raw)
        with pytest.raises(ValueError, match="question bank"):
            load_question_bank(tmp_path)


def test_duplicate_ids_across_files_are_rejected(tmp_path):
    from agent.interview_questions import load_question_bank

    root = custom_bank(tmp_path)
    (root / "references/coding-questions.yaml").write_bytes((root / "references/common-questions.yaml").read_bytes())
    with pytest.raises(ValueError, match="question bank"):
        load_question_bank(tmp_path)


def test_file_count_and_total_bytes_are_bounded(tmp_path):
    from agent.interview_questions import load_question_bank

    root = custom_bank(tmp_path)
    for i in range(17):
        (root / f"references/extra{i}-questions.yaml").write_text(yaml.safe_dump({"questions": [question(id=f"extra{i}")]}))
    with pytest.raises(ValueError, match="question bank"):
        load_question_bank(tmp_path)
    for path in root.glob("references/extra*-questions.yaml"):
        path.unlink()
    for i in range(6):
        (root / f"references/extra{i}-questions.yaml").write_text(
            yaml.safe_dump({"questions": [question(id=f"extra{i}", question="x" * 90_000)]}))
    with pytest.raises(ValueError, match="question bank"):
        load_question_bank(tmp_path)


def test_extra_unsafe_fields_rejected(tmp_path):
    from agent.interview_questions import load_question_bank

    root = custom_bank(tmp_path)
    path = root / "references/common-questions.yaml"
    path.write_text(path.read_text() + "  unknown: !!set {x: null}\n")
    with pytest.raises(ValueError, match="question bank"):
        load_question_bank(tmp_path)


def test_shipped_starter_when_custom_absent_does_not_install(tmp_path, monkeypatch):
    from agent.interview_questions import load_question_bank

    home = tmp_path / "absent-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    bank = load_question_bank()
    assert bank == load_question_bank(home)
    assert {"common", "coding", "research"} <= {q["category"] for q in bank["questions"]}
    assert len({q["id"] for q in bank["questions"]}) == len(bank["questions"])
    assert not home.exists()
    assert "skip_when" in bank["prompt"]
    assert "not automatic" in bank["prompt"].lower()


def test_environment_and_explicit_home_precedence(tmp_path, monkeypatch):
    from agent.interview_questions import load_question_bank

    first, second = tmp_path / "first", tmp_path / "second"
    custom_bank(first, [question(id="first")])
    custom_bank(second, [question(id="second")])
    monkeypatch.setenv("HERMES_HOME", str(first))
    assert load_question_bank()["questions"][0]["id"] == "first"
    assert load_question_bank(second)["questions"][0]["id"] == "second"


@pytest.mark.parametrize("kind", ["directory", "file", "broken_link"])
def test_invalid_custom_root_never_falls_back(tmp_path, kind):
    from agent.interview_questions import load_question_bank

    root = tmp_path / "skills/task-interview"
    root.parent.mkdir()
    if kind == "directory":
        root.mkdir()
    elif kind == "file":
        root.write_text("not a bank")
    else:
        root.symlink_to(tmp_path / "missing")
    with pytest.raises(ValueError, match="question bank"):
        load_question_bank(tmp_path)


def test_guidance_requires_valid_skill_frontmatter(tmp_path):
    from agent.interview_questions import load_question_bank

    root = custom_bank(tmp_path)
    for text in ("not a skill", "---\nname: task-interview\n---\nBody", "---\nname: task-interview\ndescription: Interview\n---\n"):
        (root / "SKILL.md").write_text(text)
        with pytest.raises(ValueError, match="question bank"):
            load_question_bank(tmp_path)


@pytest.mark.parametrize("component", ["home", "skills"])
@pytest.mark.parametrize("dangling", [False, True])
def test_symlinked_custom_parents_are_rejected(tmp_path, component, dangling):
    from agent.interview_questions import QuestionBankError, load_question_bank

    home = tmp_path / "home"
    custom_bank(home)
    path = home if component == "home" else home / "skills"
    external = tmp_path / "external"
    path.rename(external)
    path.symlink_to(tmp_path / "missing" if dangling else external, target_is_directory=True)
    with pytest.raises(QuestionBankError, match="question bank"):
        load_question_bank(home)


def test_reference_scan_counts_irrelevant_entries(tmp_path, monkeypatch):
    from agent import interview_questions as banks

    root = custom_bank(tmp_path)
    refs = root / "references"
    for i in range(510):
        (refs / f"irrelevant-{i}.txt").touch()
    assert banks.load_question_bank(tmp_path)["questions"] == [question()]
    for i in range(510, 600):
        (refs / f"irrelevant-{i}.txt").touch()

    original_scandir = banks.os.scandir
    visited = []

    class CountingScan:
        def __init__(self, path):
            self.entries = original_scandir(path)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.entries.close()

        def __iter__(self):
            for entry in self.entries:
                visited.append(entry.name)
                yield entry

    monkeypatch.setattr(banks.os, "scandir", CountingScan)
    with pytest.raises(banks.QuestionBankError, match="too many reference entries"):
        banks.load_question_bank(tmp_path)
    assert len(visited) == 513  # One lookahead proves the 512-entry limit was exceeded.


@pytest.mark.parametrize("component", ["references", "task-interview", "skills", "home"])
def test_references_swap_to_symlink_cannot_redirect_snapshot(tmp_path, monkeypatch, component):
    from agent import interview_questions as banks

    home = tmp_path / "home"
    root = custom_bank(home)
    external = custom_bank(tmp_path / "external", [question(id="external")])
    expected = banks.load_question_bank(home)
    source, target = {
        "references": (root / "references", external / "references"),
        "task-interview": (root, external),
        "skills": (root.parent, external.parent),
        "home": (home, external.parent.parent),
    }[component]
    original_read = banks._read
    swapped = False

    def swap_before_read(*args, **kwargs):
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(source.with_name(source.name + "-original"))
            source.symlink_to(target, target_is_directory=True)
        return original_read(*args, **kwargs)

    monkeypatch.setattr(banks, "_read", swap_before_read)
    assert banks.load_question_bank(home) == expected
    assert swapped


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS system aliases")
def test_macos_canonical_alias_is_supported(tmp_path):
    from agent.interview_questions import load_question_bank

    canonical = tmp_path.resolve()
    if canonical.parts[1:3] != ("private", "var"):
        pytest.skip("Temporary directory is not under /private/var")
    custom_bank(canonical)
    alias = Path("/").joinpath(*canonical.parts[2:])
    assert load_question_bank(alias) == load_question_bank(canonical)


def test_symlinked_references_are_rejected(tmp_path):
    from agent.interview_questions import QuestionBankError, load_question_bank

    root = custom_bank(tmp_path)
    refs = root / "references"
    refs.rename(root / "original")
    refs.symlink_to(root / "original", target_is_directory=True)
    with pytest.raises(QuestionBankError, match="question bank"):
        load_question_bank(tmp_path)


def test_examples_unknown_fields_rejected(tmp_path):
    from agent.interview_questions import load_question_bank

    root = custom_bank(tmp_path)
    path = root / "references/common-questions.yaml"
    item = question()
    item["examples"][0]["extra"] = {"not-json"}
    path.write_text(yaml.safe_dump({"questions": [item]}))
    with pytest.raises(ValueError, match="question bank"):
        load_question_bank(tmp_path)
