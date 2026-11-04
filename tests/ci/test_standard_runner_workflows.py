"""Fork CI must schedule on hosted runners without losing suite coverage."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())


def test_hosted_runner_and_checkout_contract():
    allowed = {"ubuntu-latest", "ubuntu-24.04-arm", "windows-latest", "macos-latest"}
    for path in WORKFLOWS.glob("*.y*ml"):
        for job in yaml.safe_load(path.read_text()).get("jobs", {}).values():
            runner = job.get("runs-on", "")
            if isinstance(runner, str) and runner and "${{" not in runner:
                assert runner in allowed, (path.name, runner)
            matrix = job.get("strategy", {}).get("matrix", {})
            for row in matrix.get("include", []) if isinstance(matrix, dict) else []:
                if "runner" in row:
                    assert row["runner"] in allowed, (path.name, row)
    diff = workflow("lockfile-diff.yml")["jobs"]["diff"]
    checkout = next(s for s in diff["steps"] if s.get("uses", "").startswith("actions/checkout@"))
    # Keep full ancestry for merge-base, but don't download every historic blob.
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["filter"] == "blob:none"
    assert diff["timeout-minutes"] >= 10


def test_python_matrix_partitions_the_canonical_suite_once(tmp_path):
    config = workflow("tests.yml")
    jobs = config["jobs"]
    test = jobs["test"]
    assert test["needs"] == "generate"
    assert test["strategy"]["fail-fast"] is False
    assert test["strategy"]["matrix"] == "${{ fromJSON(needs.generate.outputs.matrix) }}"
    # Use the caller's real default, not an independently maintained test count.
    trigger = config.get("on", config.get(True))
    count = trigger["workflow_call"]["inputs"]["slice_count"]["default"]
    assert 4 <= count <= 32
    ci = workflow("ci.yaml")
    caller = ci["jobs"]["tests"]
    assert caller["uses"] == "./.github/workflows/tests.yml"
    assert caller["if"] == "needs.detect.outputs.python == 'true'"
    assert "tests" in ci["jobs"]["all-checks-pass"]["needs"]
    assert "secrets" not in caller
    assert jobs["generate"]["outputs"]["matrix"] == "${{ steps.matrix.outputs.matrix }}"
    generator = next(s for s in jobs["generate"]["steps"] if s.get("id") == "matrix")
    assert f"--generate-slices ${{{{ inputs.slice_count }}}}" in generator["run"]
    run = next(s for s in test["steps"] if s.get("name", "").startswith("Run tests"))
    assert "scripts/run_tests.sh --files" in run["run"]
    assert "${{ matrix.slice.files }}" in run["env"]["TEST_FILES"]
    assert '"$TEST_FILES"' in run["run"]
    assert 1 <= int(run["env"]["HERMES_TEST_WORKERS"]) <= 4
    assert not test.get("continue-on-error", False)
    assert "--extra all --extra dev" in next(s["with"]["command"] for s in test["steps"] if s.get("name") == "Install dependencies")
    assert "python -m pytest tests/e2e/ -v --tb=short" in jobs["e2e"]["steps"][-1]["run"]

    spec = importlib.util.spec_from_file_location("ci_parallel_runner", ROOT / "scripts/run_tests_parallel.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    expected = {p.relative_to(ROOT).as_posix() for p in runner._discover_files([ROOT / "tests"])}
    assert expected
    # Exercise the actual generator CLI against both cold and warm caches.
    fixture = tmp_path / "suite"
    fixture.mkdir()
    for index in range(31):
        (fixture / f"test_{index:02}.py").write_text("def test_case():\n    assert True\n")
    for files, durations in [(runner._discover_files([ROOT / "tests"]), {}),
                             (runner._discover_files([fixture]), {str(p): i + 0.5 for i, p in enumerate(fixture.glob("*.py"))})]:
        buckets = runner._compute_lpt_slices(files, count, durations, ROOT)
        flat = [p for bucket in buckets for p in bucket]
        assert len(flat) == len(set(flat)) == len(files)
        assert set(flat) == set(files)
        assert buckets == runner._compute_lpt_slices(files, count, durations, ROOT)
    output = subprocess.check_output([sys.executable, str(ROOT / "scripts/run_tests_parallel.py"), "--generate-slices", str(count)], cwd=ROOT, text=True)
    matrix = json.loads(output)["slice"]
    assert [row["index"] for row in matrix] == list(range(1, count + 1))
    selected = [name for row in matrix for name in row["files"].split(":")]
    assert set(selected) == expected
    assert len(selected) == len(expected)
    assert all(row["files"] for row in matrix)
    # Exercise the matrix-to-runner --files boundary, not just its JSON shape.
    probe_files = runner._discover_files([fixture])
    for bucket in runner._compute_lpt_slices(probe_files, 2, {}, ROOT):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "scripts/run_tests_parallel.py"),
             "--files", ":".join(str(p) for p in bucket), "-j", "2", "--file-retries", "0"],
            cwd=ROOT, text=True, capture_output=True, timeout=60)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert f"{len(bucket)} tests passed, 0 failed" in proc.stdout
    failing = fixture / "test_failure.py"
    failing.write_text("def test_failure():\n    assert False, 'matrix failure must propagate'\n")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_tests_parallel.py"),
         "--files", str(failing), "--file-retries", "0"],
        cwd=ROOT, text=True, capture_output=True, timeout=30)
    assert proc.returncode == 1
    assert "matrix failure must propagate" in proc.stdout
    assert jobs["save-durations"]["needs"] == "test"
    assert "needs.test.result == 'success'" in jobs["save-durations"]["if"]
    assert "github.event_name == 'push'" in jobs["save-durations"]["if"]
    assert "github.ref == 'refs/heads/main'" in jobs["save-durations"]["if"]
    download = next(s for s in jobs["save-durations"]["steps"] if s.get("uses", "").startswith("actions/download-artifact@"))
    assert not download["with"].get("merge-multiple", False), "same-named JSON files must not overwrite one another"
    # Every shard starts with a full restored cache. Upload only its own
    # entries, otherwise a later shard's stale copy overwrites fresh timings.
    import os
    upload = next(s for s in test["steps"] if s.get("name") == "Upload per-slice durations")
    prepare = next(s for s in test["steps"] if s.get("name") == "Prepare per-slice durations")
    for index in range(2):
        work = tmp_path / str(index)
        work.mkdir()
        (work / "test_durations.json").write_text(json.dumps({"tests/a.py": 10 + index, "tests/b.py": 20 + index}))
        subprocess.run(["bash", "-e", "-c", prepare["run"]], cwd=work,
                       env={**os.environ, "TEST_FILES": ["tests/a.py", "tests/b.py"][index]}, check=True)
        artifact = work / upload["with"]["path"]
        destination = tmp_path / "durations" / str(index)
        destination.mkdir(parents=True)
        (destination / artifact.name).write_text(artifact.read_text())
    merge = next(s for s in jobs["save-durations"]["steps"] if s.get("name") == "Merge per-file durations")
    subprocess.run(["bash", "-e", "-c", merge["run"]], cwd=tmp_path, check=True)
    assert json.loads((tmp_path / "test_durations.json").read_text()) == {"tests/a.py": 10, "tests/b.py": 21}
