"""Exercise the local audit and real CI shell against synthetic PR ancestry."""

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def git(repo, *args, email="mapped@example.test"):
    env = dict(os.environ, GIT_AUTHOR_NAME="Fixture", GIT_AUTHOR_EMAIL=email,
               GIT_COMMITTER_NAME="Fixture", GIT_COMMITTER_EMAIL=email)
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def ancestry(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    (repo / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/audit_pr_attribution.py",
                    repo / "scripts/audit_pr_attribution.py")
    (repo / "scripts/release.py").write_text('AUTHOR_MAP = {"mapped@example.test": "fixture"}\n')
    git(repo, "add", ".")
    git(repo, "commit", "-m", "root")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "switch", "-c", "installed")
    git(repo, "commit", "--allow-empty", "-m", "installed history",
        email="historical@example.test")
    installed = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "topic")
    git(repo, "commit", "--allow-empty", "-m", "PR contribution",
        email="new@example.test")
    topic = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "installed")
    git(repo, "commit", "--allow-empty", "-m", "base advanced",
        email="base-only@example.test")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "merge", "--no-ff", "topic", "-m", "CI merge")
    merge = git(repo, "rev-parse", "HEAD")
    return repo, installed, base, topic, merge


def audit(repo, base=None):
    args = [sys.executable, "scripts/audit_pr_attribution.py"]
    if base is not None:
        args += ["--base-ref", base]
    return subprocess.run(args, cwd=repo, capture_output=True, text=True)


def ci(repo, base, event="pull_request"):
    workflow = yaml.safe_load((ROOT / ".github/workflows/contributor-check.yml").read_text())
    step = next(s for s in workflow["jobs"]["check-attribution"]["steps"]
                if s.get("id") == "check-emails")
    # Reusable workflows inherit the caller's github context, not workflow_call.
    # Resolve the actual step env so tests cannot hide a missing/wrong binding.
    context = {"${{ github.event.pull_request.base.sha }}": base,
               "${{ github.event_name }}": event}
    env = dict(os.environ, GITHUB_EVENT_NAME=event,
               GITHUB_REF="refs/heads/main" if event == "push" else "refs/pull/1/merge",
               GITHUB_OUTPUT=str(repo / "output"))
    env.update({key: context[value] for key, value in step.get("env", {}).items()})
    return subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], cwd=repo,
                          env=env, capture_output=True, text=True)


def test_main_push_inherited_caller_context_preserves_empty_scan(ancestry):
    repo, installed, base, topic, merge = ancestry
    git(repo, "switch", "main")
    git(repo, "merge", "--ff-only", merge)
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    result = ci(repo, "", event="push")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new commits to check." in result.stdout
    assert (repo / "output").read_text() == "review_status=[]\n"
    assert (repo / "review-status.json").read_text() == "review_status=[]\n"


@pytest.mark.parametrize("event", ["push", "", "workflow_call", "workflow_dispatch", "pull_request_target", "unexpected"])
def test_caller_event_policy_never_silently_skips_scan(ancestry, event):
    repo, installed, base, topic, merge = ancestry
    # Even valid PR metadata must not mask an unknown caller event. A real push
    # must still scan origin/main..HEAD, rather than unconditionally succeeding.
    result = ci(repo, base, event=event)
    assert result.returncode != 0, result.stdout + result.stderr
    assert "No new commits" not in result.stdout
    if event == "push":
        assert "historical@example.test" in result.stdout
        assert "new@example.test" in result.stdout
        assert "Unmapped contributor email(s)" in (repo / "output").read_text()
    else:
        assert "Unsupported or missing caller event" in result.stderr
        assert not (repo / "output").exists()
        assert not (repo / "review-status.json").exists()


@pytest.mark.parametrize("entrypoint,checkout", [("local", "topic"), ("local", "merge"), ("ci", "merge")])
def test_only_pr_authors_require_mapping(ancestry, checkout, entrypoint):
    repo, installed, base, topic, merge = ancestry
    git(repo, "checkout", topic if checkout == "topic" else merge)
    result = audit(repo, base) if entrypoint == "local" else ci(repo, base)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "new@example.test" in result.stdout
    assert "historical@example.test" not in result.stdout
    assert "base-only@example.test" not in result.stdout
    # Mapping the real PR author makes the same gate pass, without mapping history.
    mappings = repo / "contributors/emails"
    mappings.mkdir(parents=True)
    (mappings / "new@example.test").write_text("fixture\n")
    result = audit(repo, base) if entrypoint == "local" else ci(repo, base)
    assert result.returncode == 0, result.stdout + result.stderr
    # The default main scan still enforces the historical author on a main PR.
    if entrypoint == "local":
        result = audit(repo)
        assert result.returncode == 1
        assert "historical@example.test" in result.stdout


@pytest.mark.parametrize("case", ["missing", "malformed", "unknown", "unrelated", "wrong-main", "wrong-head", "log-error", "local-missing", "local-unrelated", "local-log-error"])
def test_invalid_base_or_scan_fails_closed(ancestry, case):
    repo, installed, base, topic, merge = ancestry
    unrelated = ""
    old_path = os.environ["PATH"]
    if "unrelated" in case:
        git(repo, "switch", "--orphan", "unrelated")
        git(repo, "commit", "--allow-empty", "-m", "unrelated")
        unrelated = git(repo, "rev-parse", "HEAD")
        git(repo, "checkout", merge)
    bases = {"missing": "", "malformed": "HEAD", "unknown": "0" * 40,
             "wrong-main": git(repo, "rev-parse", "origin/main"), "wrong-head": merge}
    selected = unrelated if "unrelated" in case else bases.get(case, base)
    if case == "local-missing":
        git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    if "log-error" in case:
        # Real git for ancestry, failing git-log child: never turn scan errors green.
        bin_dir = repo / "bin"
        bin_dir.mkdir()
        wrapper = bin_dir / "git"
        wrapper.write_text(f'#!/bin/sh\nif [ "$1" = log ]; then exit 23; fi\nexec "{shutil.which("git")}" "$@"\n')
        wrapper.chmod(0o755)
        os.environ["PATH"] = str(bin_dir) + os.pathsep + old_path
    try:
        result = audit(repo, None if case == "local-missing" else selected) if case.startswith("local-") else ci(repo, selected)
    finally:
        if "log-error" in case:
            os.environ["PATH"] = old_path
    assert result.returncode != 0, result.stdout + result.stderr
    assert "All contributor emails" not in result.stdout
    assert "No new commits" not in result.stdout
    assert not (repo / "output").exists()
