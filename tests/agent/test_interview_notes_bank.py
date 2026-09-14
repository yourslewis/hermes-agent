"""Editable Notes banks: real files, safe snapshots and append-only writes."""
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from agent.interview_questions import QuestionBankError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX nofollow filesystem API")


def question(**changes) -> dict:
    return dict({
        "id": "success", "category": "common", "priority": "high",
        "status": "active", "tags": ["auto-generated"],
        "question": "What result would count as success?",
        "ask_when": "Success is unclear.", "skip_when": "Success is measurable.",
        "resolves": "Acceptance criterion.", "choices": [], "examples": [],
    }, **changes)


def markdown(**changes):
    item = question(**changes)
    import yaml
    meta = {k: item[k] for k in ("id", "category", "priority", "status", "tags")}
    return "---\n" + yaml.safe_dump(meta, sort_keys=False) + "---\n\n" + "\n\n".join(
        "## " + label + "\n" + item[key]
        for label, key in (("Question", "question"), ("Ask when", "ask_when"),
                           ("Skip when", "skip_when"), ("Resolves", "resolves"))
    ) + "\n"


def test_parse_minimal_markdown_and_render_round_trip():
    from agent.interview_notes_bank import parse_question, render_question

    assert parse_question(markdown()) == question()
    item = question(choices=["Report", "Prototype"], examples=[{
        "task": "Improve dashboard", "good": "Which comparison?", "bad": "More details?"
    }], question="What result?\n\nInclude metrics: yes/no, 中文.")
    rendered = render_question(item)
    assert rendered.startswith("---\nid: success\n")
    assert "## Ask when" in rendered
    assert parse_question(rendered) == item


def bank_files(tmp_path):
    root = tmp_path / "03-Question-Bank"
    root.mkdir()
    (root / "Guidance.md").write_text("# Guidance\nAsk only unresolved questions.\n")
    (root / "success.md").write_text(markdown())
    return root


def test_load_active_only_stable_read_only_snapshot(tmp_path):
    from agent.interview_notes_bank import load_notes_bank

    root = bank_files(tmp_path)
    (root / "draft.md").write_text(markdown(id="draft", status="proposed"))
    (root / "no.md").write_text(markdown(id="no", status="rejected"))
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    bank = load_notes_bank(root)
    runtime_question = {k: v for k, v in question().items() if k not in {"status", "tags"}}
    assert bank["questions"] == [runtime_question]
    assert set(bank) == {"version", "prompt", "questions"}
    assert "untrusted data" in bank["prompt"].lower()
    assert "permission" in bank["prompt"].lower()
    assert bank["version"] == hashlib.sha256(bank["prompt"].encode()).hexdigest()
    assert bank == load_notes_bank(root)
    assert before == {p.name: p.read_bytes() for p in root.iterdir()}
    (root / "success.md").write_text(markdown(question="Updated question?"))
    assert load_notes_bank(root)["version"] != bank["version"]


@pytest.mark.parametrize("damage", [
    "missing-guidance", "empty-guidance", "invalid-yaml", "alias", "duplicate-key",
    "unknown-permission", "duplicate-section", "duplicate-id", "wrong-filename",
    "symlink", "symlink-guidance", "fifo", "oversize", "invalid-utf8", "no-active",
    "malformed-proposed", "total-size", "too-many-entries", "ancestor-symlink",
])
def test_invalid_notes_fail_closed(tmp_path, damage):
    from agent.interview_notes_bank import load_notes_bank

    root = bank_files(tmp_path)
    path = root / "success.md"
    if damage == "missing-guidance":
        (root / "Guidance.md").unlink()
    elif damage == "empty-guidance":
        (root / "Guidance.md").write_text("")
    elif damage == "invalid-yaml":
        path.write_text(markdown().replace("id: success", "id: !!python/object:os.system {}"))
    elif damage == "alias":
        path.write_text(markdown().replace("id: success", "id: &x success\nother: *x"))
    elif damage == "duplicate-key":
        path.write_text(markdown().replace("id: success", "id: success\nid: other"))
    elif damage == "unknown-permission":
        path.write_text(markdown().replace("id: success", "id: success\nallow_write: true"))
    elif damage == "duplicate-section":
        path.write_text(markdown() + "\n## Question\nInjected\n")
    elif damage == "duplicate-id":
        (root / "second.md").write_text(markdown())
    elif damage == "wrong-filename":
        path.rename(root / "unrelated.md")
    elif damage in {"symlink", "symlink-guidance"}:
        path = path if damage == "symlink" else root / "Guidance.md"
        external = tmp_path / "external.md"
        path.rename(external)
        path.symlink_to(external)
    elif damage == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif damage == "oversize":
        path.write_bytes(b"x" * 100_001)
    elif damage == "invalid-utf8":
        path.write_bytes(b"\xff")
    elif damage == "no-active":
        path.write_text(markdown(status="proposed"))
    elif damage == "malformed-proposed":
        (root / "draft.md").write_text(markdown(id="draft", status="proposed", resolves=""))
    elif damage == "total-size":
        for i in range(6):
            (root / f"large{i}.md").write_text(markdown(id=f"large{i}", question="x" * 90_000))
    elif damage == "too-many-entries":
        for i in range(513):
            (root / f"ignored{i}.txt").touch()
    elif damage == "ancestor-symlink":
        actual = tmp_path / "real"
        actual.mkdir()
        root.rename(actual / root.name)
        link = tmp_path / "linked"
        link.symlink_to(actual, target_is_directory=True)
        root = link / root.name
    with pytest.raises(QuestionBankError):
        load_notes_bank(root)


def test_explicit_cache_recovers_last_good_with_warning(tmp_path):
    from agent.interview_notes_bank import load_notes_bank

    root = bank_files(tmp_path)
    cache = tmp_path / "last-good.json"
    good = load_notes_bank(root, cache_path=cache)
    assert cache.is_file()
    cached_bytes = cache.read_bytes()
    (root / "success.md").write_text("broken edit")
    stale = load_notes_bank(root, cache_path=cache)
    assert {k: stale[k] for k in good} == good
    assert "last-good" in stale["warning"]
    assert cache.read_bytes() == cached_bytes
    (root / "success.md").write_text(markdown(question="Fixed?"))
    assert "warning" not in load_notes_bank(root, cache_path=cache)
    assert cache.read_bytes() != cached_bytes


@pytest.mark.parametrize("damage", ["bad-json", "invalid-item", "wrong-root", "bad-version", "duplicate-key", "symlink"])
def test_invalid_cache_cannot_rescue_invalid_notes(tmp_path, damage):
    from agent.interview_notes_bank import load_notes_bank

    root = bank_files(tmp_path)
    cache = tmp_path / "last-good.json"
    load_notes_bank(root, cache)
    payload = json.loads(cache.read_text())
    if damage == "bad-json":
        cache.write_text("{")
    elif damage == "invalid-item":
        payload["items"][0]["allow_write"] = True
        cache.write_text(json.dumps(payload))
    elif damage == "wrong-root":
        payload["root"] = str(tmp_path / "other")
        cache.write_text(json.dumps(payload))
    elif damage == "bad-version":
        payload["version"] = "fake"
        cache.write_text(json.dumps(payload))
    elif damage == "duplicate-key":
        cache.write_text(cache.read_text().replace('"root":', '"root": "duplicate", "root":', 1))
    elif damage == "symlink":
        external = tmp_path / "elsewhere"
        cache.rename(external)
        cache.symlink_to(external)
    (root / "success.md").write_text("broken edit")
    with pytest.raises(QuestionBankError):
        load_notes_bank(root, cache)


def test_cache_must_not_be_inside_notes_and_symlink_never_overwritten(tmp_path):
    from agent.interview_notes_bank import load_notes_bank

    root = bank_files(tmp_path)
    with pytest.raises(QuestionBankError):
        load_notes_bank(root, root / "cache.json")
    assert not (root / "cache.json").exists()
    target = tmp_path / "target"
    target.write_text("untouched")
    link = tmp_path / "cache-link"
    link.symlink_to(target)
    with pytest.raises(QuestionBankError):
        load_notes_bank(root, link)
    assert target.read_text() == "untouched"


def test_read_pins_root_and_rejects_leaf_swap(tmp_path, monkeypatch):
    from agent import interview_notes_bank as banks

    root = bank_files(tmp_path)
    expected = banks.load_notes_bank(root)
    moved = tmp_path / "moved"
    external = tmp_path / "external"
    external.mkdir()
    (external / "Guidance.md").write_text("unrelated")
    (external / "success.md").write_text(markdown(question="External question?"))
    original_read = banks._read
    swapped = False
    def swap_root(name, fd):
        nonlocal swapped
        if not swapped:
            swapped = True
            root.rename(moved)
            root.symlink_to(external, target_is_directory=True)
        return original_read(name, fd)
    monkeypatch.setattr(banks, "_read", swap_root)
    assert banks.load_notes_bank(root) == expected
    monkeypatch.setattr(banks, "_read", original_read)
    root.unlink()
    moved.rename(root)
    def swap_leaf(name, fd):
        if name == "success.md":
            (root / name).unlink()
            (root / name).symlink_to(external / name)
        return original_read(name, fd)
    monkeypatch.setattr(banks, "_read", swap_leaf)
    with pytest.raises(QuestionBankError):
        banks.load_notes_bank(root)


def test_concurrent_writers_deduplicate_without_notes_sidecars(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from agent.interview_notes_bank import add_question

    root = bank_files(tmp_path)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda i: add_question(root, question(
            id=f"race{i}", question="Shared new question?")), range(4)))
    assert sorted(r["status"] for r in results) == ["created", "duplicate", "duplicate", "duplicate"]
    assert len(list(root.iterdir())) == 3


def test_parse_rejects_deep_yaml_and_duplicate_examples_keys():
    from agent.interview_notes_bank import parse_question

    for yaml_line in (
        "examples: " + "[" * 30 + "]" * 30,
        "examples: [{task: x, task: y, good: a, bad: b}]",
        "examples: !!python/object/apply:os.system [touch nope]",
    ):
        with pytest.raises(QuestionBankError):
            parse_question(markdown().replace("id: success", "id: success\n" + yaml_line))


def test_root_path_fails_as_bank_not_index_error():
    from agent.interview_notes_bank import load_notes_bank

    with pytest.raises(QuestionBankError):
        load_notes_bank("/")


@pytest.mark.parametrize("changes", [
    {"priority": []}, {"status": []}, {"tags": "auto-generated"},
    {"choices": ["a"] * 5}, {"examples": [{"task": "x"}]},
    {"category": "\ud800"}, {"id": "bad id"}, {"resolves": ""},
])
def test_render_invalid_schema_is_closed(changes):
    from agent.interview_notes_bank import render_question

    with pytest.raises(QuestionBankError):
        render_question(question(**changes))


def test_add_respects_actual_disk_byte_and_entry_limits(tmp_path):
    from agent.interview_notes_bank import add_question

    root = bank_files(tmp_path)
    # Comments count toward the disk budget, even though rendering omits them.
    for i in range(5):
        raw = markdown(id=f"large{i}", question=f"Question {i}?")
        raw = raw.replace("---\n", "---\n#" + "x" * 99_000 + "\n", 1)
        (root / f"large{i}.md").write_text(raw)
    with pytest.raises(QuestionBankError):
        add_question(root, question(id="overflow", question="x" * 10_000))
    assert not (root / "overflow.md").exists()
    for path in root.glob("large*.md"):
        path.unlink()
    for i in range(510):
        (root / f"ignored{i}").touch()
    with pytest.raises(QuestionBankError):
        add_question(root, question(id="overflow", question="Another question?"))
    assert not (root / "overflow.md").exists()


def test_add_is_exclusive_and_deduplicates_content_across_statuses(tmp_path):
    from agent.interview_notes_bank import add_question, parse_question

    root = bank_files(tmp_path)
    original = (root / "success.md").read_bytes()
    item = question(id="new", question="What deadline matters?", status="proposed")
    result = add_question(root, item)
    assert result == {"status": "created", "path": str(root / "new.md")}
    assert parse_question(Path(result["path"]).read_text()) == item
    assert add_question(root, item)["status"] == "duplicate"
    assert add_question(root, question(id="alternate", question="  WHAT   deadline matters?  ")) == {
        "status": "duplicate", "path": str(root / "new.md")}
    collision = add_question(root, question(question="Different text with same id?"))
    assert collision == {"status": "exists", "path": str(root / "success.md")}
    assert (root / "success.md").read_bytes() == original
    assert not (root / "alternate.md").exists()


def test_add_defaults_to_proposal_and_rejects_unsafe_root_and_id(tmp_path):
    from agent.interview_notes_bank import add_question, parse_question

    root = bank_files(tmp_path)
    item = question(id="draft", question="New question?")
    del item["status"]
    del item["tags"]
    add_question(root, item)
    stored = parse_question((root / "draft.md").read_text())
    assert stored["status"] == "proposed"
    assert stored["tags"] == ["auto-generated"]
    with pytest.raises(QuestionBankError):
        add_question(root, question(id="../escape"))
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(QuestionBankError):
        add_question(link, question(id="outside", question="Other?"))
    assert not (root / "outside.md").exists()


def test_add_never_follows_target_link_or_overwrites_race_winner(tmp_path, monkeypatch):
    from agent import interview_notes_bank as banks

    root = bank_files(tmp_path)
    original_open = banks.os.open
    target = root / "racing.md"
    def racing_open(name, flags, *args, **kwargs):
        if name == "racing.md" and flags & os.O_CREAT:
            target.write_text("won the race")
        return original_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(banks.os, "open", racing_open)
    result = banks.add_question(root, question(id="racing", question="Race?"))
    assert result["status"] == "exists"
    assert target.read_text() == "won the race"


def test_initialize_migrates_explicitly_and_never_overwrites(tmp_path):
    from agent.interview_notes_bank import initialize_notes_bank, load_notes_bank
    from agent.interview_questions import load_question_bank

    shipped = load_question_bank(tmp_path / "empty-profile")
    root = tmp_path / "03-Question-Bank"
    assert not root.exists()
    result = initialize_notes_bank(root, shipped)
    assert result["status"] == "created"
    bank = load_notes_bank(root)
    assert bank["questions"] == sorted(
        [dict(choices=[], **q) if "choices" not in q else q for q in shipped["questions"]],
        key=lambda q: q["id"],
    )
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    (root / "Guidance.md").write_text("My edits")
    assert initialize_notes_bank(root, shipped)["status"] == "exists"
    assert (root / "Guidance.md").read_text() == "My edits"
    assert len(list(root.iterdir())) == len(before)


def test_initialize_validates_before_mutation_and_rejects_link(tmp_path):
    from agent.interview_notes_bank import initialize_notes_bank

    root = tmp_path / "03-Question-Bank"
    with pytest.raises(QuestionBankError):
        initialize_notes_bank(root, {"version": "bad", "prompt": "Guidance", "questions": [question(id="../x")]})
    assert not root.exists()
    real = tmp_path / "real"
    real.mkdir()
    root.symlink_to(real, target_is_directory=True)
    from agent.interview_questions import load_question_bank
    with pytest.raises(QuestionBankError):
        initialize_notes_bank(root, load_question_bank(tmp_path / "absent"))
    assert not list(real.iterdir())


def test_profile_config_selects_notes_without_mutation_and_explicit_home_wins(tmp_path, monkeypatch):
    from agent.interview_questions import load_question_bank

    root = bank_files(tmp_path)
    home = tmp_path / "profile"
    home.mkdir()
    config = home / "question-learning.json"
    config.write_text(json.dumps({"enabled": True, "notes_root": str(root), "since": "123.4"}))
    monkeypatch.setenv("HERMES_HOME", str(home))
    expected = load_question_bank(home)
    assert expected["questions"][0]["id"] == "success"
    assert load_question_bank() == expected
    assert list(home.iterdir()) == [config]
    assert len(load_question_bank(tmp_path / "absent")["questions"]) > 1
    config.write_text(json.dumps({"enabled": False, "notes_root": str(root)}))
    assert load_question_bank(home) == expected
    assert load_question_bank() == expected
    assert list(home.iterdir()) == [config]


@pytest.mark.parametrize("raw", [
    '{', '[]', '{"enabled": "yes"}', '{"enabled": true}',
    '{"enabled": true, "notes_root": "relative"}',
    '{"enabled": false, "enabled": true}',
    '{"enabled": true, "notes_root": "/absent-question-bank"}',
])
def test_invalid_profile_notes_config_never_selects_shipped(tmp_path, raw):
    from agent.interview_questions import load_question_bank

    (tmp_path / "question-learning.json").write_text(raw)
    with pytest.raises(QuestionBankError):
        load_question_bank(tmp_path)


def test_configured_cache_is_explicit_local_and_surfaces_warning(tmp_path):
    from agent.interview_questions import load_question_bank

    root = bank_files(tmp_path)
    home = tmp_path / "profile"
    home.mkdir()
    cache = home / "last-good.json"
    (home / "question-learning.json").write_text(json.dumps({
        "enabled": True, "notes_root": str(root), "cache_path": str(cache),
    }))
    before = load_question_bank(home)
    assert cache.is_file()
    (root / "success.md").write_text("broken")
    after = load_question_bank(home)
    assert after["version"] == before["version"]
    assert after["warning"]


def test_config_symlink_is_not_followed(tmp_path):
    from agent.interview_questions import load_question_bank

    external = tmp_path / "external.json"
    external.write_text('{"enabled": false}')
    (tmp_path / "question-learning.json").symlink_to(external)
    with pytest.raises(QuestionBankError):
        load_question_bank(tmp_path)
