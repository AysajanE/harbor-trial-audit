import csv
import json
import shutil
from pathlib import Path

import pytest

from tools.harbor_trial_audit.audit import audit, main


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def trial(tmp_path):
    path = tmp_path / "job" / "checkpoint__abc1234"
    config = {
        "trial_name": path.name,
        "task": {"path": "retained-task"},
        "environment": {"type": "modal"},
        "artifacts": ["/app/output"],
    }
    write_json(path / "config.json", config)
    write_json(path / "result.json", {
        "trial_name": path.name, "task_name": "checkpoint", "config": config,
        "verifier_result": {"rewards": {"reward": 0.0}},
        "exception_info": None,
    })
    write_json(path.parent / "result.json", {"n_total_trials": 1, "stats": {}})
    write_json(path.parent / "lock.json", {"harbor": {"version": "0.24.0"}})
    return path


def manifest(trial, status="ok", **extra):
    entry = {
        "source": "/app/output", "destination": "artifacts/app/output",
        "type": "directory", "service": None, **extra,
    }
    if status is not None:
        entry["status"] = status
    write_json(trial / "artifacts" / "manifest.json", [entry])


def payload(trial, name="model.bin", data=b"weights"):
    path = trial / "artifacts" / "app" / "output" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def row(trial):
    report = audit([trial.parent])
    assert len(report["trials"]) == 1
    return report["trials"][0]


@pytest.mark.parametrize("status", ["failed", "empty", "skipped", "error"])
@pytest.mark.parametrize("has_payload", [True, False])
def test_graded_on_non_success_including_zero_reward(trial, status, has_payload):
    manifest(trial, status)
    if has_payload:
        payload(trial)  # A partial copy does not erase a recorded failure.
    else:
        (trial / "artifacts/app/output").mkdir(parents=True)
    result = row(trial)
    assert result["classification"] == "GRADED_ON_FAILED_ARTIFACT"
    assert result["reward"] == {"reward": 0.0}
    assert result["provider"] == "modal"
    assert result["harbor_version"] == "0.24.0"
    assert result["artifacts"][0]["status"] == status
    assert result["artifacts"][0]["file_count"] == int(has_payload)
    assert result["artifacts"][0]["bytes_present"] == (7 if has_payload else 0)
    summary = audit([trial.parent])["summary"]
    assert summary["zero_reward_non_ok"]["GRADED_ON_FAILED_ARTIFACT"] == 1
    assert main([str(trial)]) == 1


@pytest.mark.parametrize("status", ["ok", "success", None])
@pytest.mark.parametrize("contents", [None, b""])
def test_graded_on_missing_or_zero_byte_copy(trial, status, contents):
    manifest(trial, status)
    if contents is not None:
        payload(trial, data=contents)
    result = row(trial)
    assert result["classification"] == "GRADED_ON_EMPTY_ARTIFACT"
    assert result["artifacts"][0]["bytes_present"] == 0


def test_no_manifest_still_counts_declared_copy(trial):
    payload(trial)
    result = row(trial)
    assert result["classification"] == "NO_MANIFEST"
    assert result["artifacts"][0]["file_count"] == 1
    assert any("no artifact manifest in any scope" in note for note in result["notes"])


def test_ok_counts_nested_files_and_accepts_unknown_status_with_bytes(trial):
    manifest(trial, None)
    payload(trial)
    payload(trial, "nested/log.txt", b"log")
    result = row(trial)
    assert result["classification"] == "OK"
    assert result["artifacts"][0]["status"] is None
    assert result["artifacts"][0]["file_count"] == 2
    assert result["artifacts"][0]["bytes_present"] == 10


@pytest.mark.parametrize("status,expected", [
    ("failed", "GRADED_ON_FAILED_ARTIFACT"),
    ("skipped", "GRADED_ON_FAILED_ARTIFACT"),
    ("ok", "NO_ARTIFACTS_DECLARED"),
    ("empty", "NO_ARTIFACTS_DECLARED"),
    ("success", "NO_ARTIFACTS_DECLARED"),
])
@pytest.mark.parametrize("source", ["/logs/artifacts", "C:/logs/artifacts"])
def test_manifest_only_task_declaration_and_implicit_convention(trial, status, expected, source):
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        config = data["config"] if name == "result.json" else data
        config["artifacts"] = []
        write_json(trial / name, data)
    manifest(trial, "failed")
    assert row(trial)["classification"] == "GRADED_ON_FAILED_ARTIFACT"
    write_json(trial / "artifacts" / "manifest.json", [{
        "source": source, "destination": "artifacts/logs/artifacts",
        "type": "directory", "status": status, "service": None,
    }])
    (trial / "artifacts/logs/artifacts").mkdir(parents=True)
    result = row(trial)
    assert result["classification"] == expected
    assert result["reward"] == {"reward": 0.0}
    if expected == "GRADED_ON_FAILED_ARTIFACT":
        assert result["artifacts"][0]["status"] == status
        assert result["artifacts"][0]["file_count"] == 0
        assert result["artifacts"][0]["bytes_present"] == 0
    else:
        assert result["artifacts"] == []
    assert main([str(trial)]) == int(expected == "GRADED_ON_FAILED_ARTIFACT")


@pytest.mark.parametrize("task_source", [{"git_url": "https://example.test/task.git"}, {"name": "org/task"}])
@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("step", [False, True])
def test_unrecorded_task_path_and_absent_manifests_are_noted(trial, task_source, declared, step):
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        config = data.get("config", data)
        config["task"] = {"path": None, **task_source}
        config["artifacts"] = ["/app/output"] if declared else []
        if step and name == "result.json":
            data["verifier_result"] = None
            data["step_results"] = [{
                "step_name": "train", "verifier_result": {"rewards": {"reward": 0.0}},
            }]
        write_json(trial / name, data)
    result = row(trial)
    assert result["classification"] == ("NO_MANIFEST" if declared else "NO_ARTIFACTS_DECLARED")
    assert "task path not recorded for git/package task; declarations may be absent" in result["notes"]
    assert any("no artifact manifest in any scope" in note for note in result["notes"])
    assert main([str(trial)]) == int(declared)


def test_task_path_note_requires_no_recorded_path_and_manifest_note_requires_no_manifest(trial):
    data = json.loads((trial / "result.json").read_text())
    data["config"]["task"]["path"] = None  # config.json still records a path.
    data["step_results"] = [
        {"step_name": name, "verifier_result": {"rewards": {"reward": 0.0}}}
        for name in ("train", "test")
    ]
    write_json(trial / "result.json", data)
    manifest(trial / "steps/train")
    payload(trial / "steps/train")
    result = row(trial)
    assert result["classification"] == "NO_MANIFEST"  # The test step lacks one.
    assert not any("task path not recorded" in note for note in result["notes"])
    assert not any("no artifact manifest in any scope" in note for note in result["notes"])


def test_task_toml_declaration_and_custom_destination(trial):
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        config = data.get("config", data)
        config["artifacts"] = []
        write_json(trial / name, data)
    task = trial / "retained-task" / "task.toml"
    task.parent.mkdir()
    task.write_text('artifacts = [{source = "/app/output", destination = "saved"}]\n')
    assert row(trial)["classification"] == "NO_MANIFEST"
    manifest(trial, destination="artifacts/saved")
    (trial / "artifacts" / "saved").mkdir()
    (trial / "artifacts" / "saved" / "model").write_bytes(b"ok")
    assert row(trial)["classification"] == "OK"


def test_relative_task_path_tries_roots_after_trial(trial, tmp_path):
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        config = data.get("config", data)
        config["artifacts"] = []
        config["task"]["path"] = "tasks/foo"
        write_json(trial / name, data)
    first = tmp_path / "a-root"
    last = tmp_path / "z-root"
    first.mkdir()
    last.mkdir()
    roots = [trial.parent, first, last]
    for base, source in ((last, "/app/last"), (first, "/app/first"), (trial, "/app/trial")):
        task = base / "tasks/foo/task.toml"
        task.parent.mkdir(parents=True)
        task.write_text(f'artifacts = ["{source}"]\n')
        result = audit(roots)["trials"][0]
        assert result["classification"] == "NO_MANIFEST"
        assert [item["source"] for item in result["artifacts"]] == [source]
        assert not any("Task definition" in note for note in result["notes"])


def test_relative_task_path_skips_non_directory_root_candidate(trial, tmp_path):
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        config = data.get("config", data)
        config["artifacts"] = []
        config["task"]["path"] = "tasks/foo"
        write_json(trial / name, data)
    first = tmp_path / "a-root"
    first.mkdir()
    (first / "tasks").write_text("not a directory")
    last = tmp_path / "z-root"
    task = last / "tasks/foo/task.toml"
    task.parent.mkdir(parents=True)
    task.write_text('artifacts = ["/app/from-task"]\n')
    result = audit([trial.parent, first, last])["trials"][0]
    assert result["classification"] == "NO_MANIFEST"
    assert [item["source"] for item in result["artifacts"]] == ["/app/from-task"]


@pytest.mark.parametrize("unsafe", ["directory-link", "file-link", "directory"])
def test_relative_task_path_root_candidate_keeps_safety_checks(trial, tmp_path, unsafe):
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        data.get("config", data)["task"]["path"] = "tasks/foo"
        write_json(trial / name, data)
    root = tmp_path / "root"
    task = root / "tasks/foo/task.toml"
    task.parent.mkdir(parents=True)
    if unsafe == "directory-link":
        task.parent.rmdir()
        task.parent.symlink_to(trial, target_is_directory=True)
    elif unsafe == "file-link":
        task.symlink_to(trial / "config.json")
    else:
        task.mkdir()
    result = audit([trial.parent, root])["trials"][0]
    assert result["classification"] == "UNREADABLE"
    assert ("not a regular file" if unsafe == "directory" else "symlink not followed") in result["reason"]


def test_declared_artifact_omitted_from_manifest_is_empty(trial):
    write_json(trial / "artifacts" / "manifest.json", [])
    assert row(trial)["classification"] == "GRADED_ON_EMPTY_ARTIFACT"


@pytest.mark.parametrize("destination", [None, "./saved//"])
def test_same_source_at_missing_destination_stays_visible(trial, destination):
    declarations = [
        {"source": "/app/output", "service": "main", "destination": destination},
        {"source": "/app/output/", "destination": "backup"},
    ]
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        data.get("config", data)["artifacts"] = declarations
        write_json(trial / name, data)
    collected = "artifacts/./saved//" if destination else "artifacts/./app//output/"
    manifest(trial, destination=collected)
    copy = trial / collected / "model.bin"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"weights")
    result = row(trial)
    assert result["classification"] == "GRADED_ON_EMPTY_ARTIFACT"
    assert [(item["destination"], item["status"], item["bytes_present"])
            for item in result["artifacts"]] == [
        ("artifacts/saved" if destination else "artifacts/app/output", "ok", 7),
        ("artifacts/backup", None, 0),
    ]
    assert main([str(trial)]) == 1
    backup = trial / "artifacts/backup/model.bin"
    backup.parent.mkdir()
    backup.write_bytes(b"weights")
    assert row(trial)["classification"] == "OK"


def test_manifest_itself_does_not_make_root_artifact_nonempty(trial):
    manifest(trial, destination="artifacts")
    assert row(trial)["classification"] == "GRADED_ON_EMPTY_ARTIFACT"


def test_no_reward_is_never_zero_or_a_graded_claim(trial):
    manifest(trial, "failed")
    data = json.loads((trial / "result.json").read_text())
    data["verifier_result"] = None
    write_json(trial / "result.json", data)
    result = row(trial)
    assert result["reward"] is None
    assert result["classification"] == "UNREADABLE"
    assert "no verifier reward" in result["reason"]
    assert not any(audit([trial])["summary"]["zero_reward_non_ok"].values())


@pytest.mark.parametrize("target,contents", [
    ("result.json", "{"), ("config.json", "[]"),
    ("artifacts/manifest.json", "{}"),
    ("artifacts/manifest.json", '[{"source": 3}]'),
    ("lock.json", "{"),
    ("artifacts/manifest.json", '[{"source":"/x","source":"/y"}]'),
])
def test_unreadable_metadata_has_reason(trial, target, contents):
    manifest(trial)
    payload(trial)
    (trial / target).write_text(contents)
    result = row(trial)
    assert result["classification"] == "UNREADABLE"
    assert result["reason"]


@pytest.mark.parametrize("target,contents", [
    ("config.json", "null"), ("lock.json", "null"),
    ("artifacts/manifest.json", "null"),
    ("artifacts/manifest.json", '[{"source":"/app/output","destination":"."}]'),
    ("artifacts/manifest.json", '[{"source":"/app/output","destination":"../outside"}]'),
    ("artifacts/manifest.json", '[{"source":"/app/output","destination":"/outside"}]'),
])
def test_null_and_unsafe_metadata_are_unreadable(trial, target, contents):
    manifest(trial)
    payload(trial)
    (trial / target).write_text(contents)
    assert row(trial)["classification"] == "UNREADABLE"


def test_recorded_provider_must_be_text(trial):
    data = json.loads((trial / "result.json").read_text())
    data["config"]["environment"]["type"] = ["modal"]
    write_json(trial / "result.json", data)
    assert row(trial)["classification"] == "UNREADABLE"


@pytest.mark.parametrize("reward", [True, "0", None])
def test_malformed_reward_is_unreadable(trial, reward):
    data = json.loads((trial / "result.json").read_text())
    data["verifier_result"]["rewards"] = {"reward": reward}
    write_json(trial / "result.json", data)
    assert row(trial)["classification"] == "UNREADABLE"


def test_partial_file_inventory_check_is_unavailable(trial):
    # Harbor's manifest has no source file inventory, sizes, or hashes.
    # One arrived file cannot prove whether others were dropped (#3925).
    manifest(trial)
    payload(trial)
    assert row(trial)["classification"] == "OK"


@pytest.fixture
def partial_trial(trial):
    # Synthetic #3925 pattern: the manifest says ok, but only two files arrived.
    manifest(trial)
    payload(trial, "solve.py", b"pass\n")
    payload(trial, "model-1.bin")
    (trial / "verifier").mkdir()
    return trial


@pytest.mark.parametrize("status", ["ok", None])
@pytest.mark.parametrize("log", ["test-stdout.txt", "test-stderr.txt"])
def test_suspect_partial_artifact_reports_file_and_line(partial_trial, status, log):
    manifest(partial_trial, status)
    line = "FileNotFoundError: [Errno 2] No such file or directory: 'artifacts/app/output/model-2.bin'"
    (partial_trial / "verifier" / log).write_text("Checking models\n" + line + "\n")
    result = row(partial_trial)
    assert result["classification"] == "SUSPECT_PARTIAL_ARTIFACT"
    assert result["reward"] == {"reward": 0.0}
    assert f"verifier/{log}:2: {line}" in result["reason"]
    summary = audit([partial_trial])["summary"]
    assert summary["zero_reward_non_ok"]["SUSPECT_PARTIAL_ARTIFACT"] == 1
    classes = list(summary["by_classification"])
    assert classes.index("GRADED_ON_EMPTY_ARTIFACT") < classes.index("SUSPECT_PARTIAL_ARTIFACT") < classes.index("OK")


@pytest.mark.parametrize("artifact_type", ["file", "directory"])
@pytest.mark.parametrize("path", ["source", "relative", "absolute"])
def test_exact_declared_paths_are_suspect(trial, artifact_type, path):
    source = "/app/model.bin" if artifact_type == "file" else "/app/output"
    destination = "artifacts" + source
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        data.get("config", data)["artifacts"] = [source]
        write_json(trial / name, data)
    manifest(trial, "success", source=source, destination=destination, type=artifact_type)
    copy = trial / destination
    if artifact_type == "directory":
        copy = copy / "model.bin"
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.write_bytes(b"weights")
    missing = source if path == "source" else destination if path == "relative" else str(trial / destination)
    line = f"FileNotFoundError: {missing}"
    (trial / "verifier").mkdir()
    (trial / "verifier/test-stdout.txt").write_text(line + "\n")
    result = row(trial)
    assert result["classification"] == "SUSPECT_PARTIAL_ARTIFACT"
    assert result["artifacts"][0]["status"] == "success"
    assert result["artifacts"][0]["bytes_present"] == 7
    assert f"verifier/test-stdout.txt:1: {line}" in result["reason"]


@pytest.mark.parametrize("path", ["relative", "absolute"])
def test_bare_artifacts_root_is_not_suspect(trial, path):
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        data.get("config", data)["artifacts"] = [{"source": "/app/output", "destination": "."}]
        write_json(trial / name, data)
    manifest(trial, destination="artifacts")
    payload(trial)
    missing = "artifacts" if path == "relative" else str(trial / "artifacts")
    (trial / "verifier").mkdir()
    (trial / "verifier/test-stdout.txt").write_text(f"FileNotFoundError: {missing}\n")
    assert row(trial)["classification"] == "OK"


@pytest.mark.parametrize("missing", [
    "/unrelated/model-2.bin", "artifacts/app/output-other/model-2.bin",
    "artifacts/app/output/../outside.bin",
])
def test_missing_file_outside_destination_is_not_suspect(partial_trial, missing):
    (partial_trial / "verifier" / "test-stdout.txt").write_text(
        f"FileNotFoundError: [Errno 2] No such file or directory: '{missing}'\n"
    )
    assert row(partial_trial)["classification"] == "OK"


def test_partial_artifact_without_error_message_is_not_detectable(partial_trial):
    (partial_trial / "verifier" / "test-stdout.txt").write_text("Checking models\n")
    assert row(partial_trial)["classification"] == "OK"


@pytest.mark.parametrize("path", ["absolute", "source", "shell"])
def test_missing_paths_use_collected_or_verifier_destinations(partial_trial, path):
    missing = str(partial_trial / "artifacts/app/output/model-2.bin") if path == "absolute" else "/app/output/model-2.bin"
    line = f"cat: {missing}: No such file or directory" if path == "shell" else f"FileNotFoundError: [Errno 2] No such file or directory: '{missing}'"
    (partial_trial / "verifier" / "test-stdout.txt").write_text(line + "\n")
    assert row(partial_trial)["classification"] == "SUSPECT_PARTIAL_ARTIFACT"


@pytest.mark.parametrize("condition,expected", [
    ("failed", "GRADED_ON_FAILED_ARTIFACT"),
    ("empty", "GRADED_ON_EMPTY_ARTIFACT"),
    ("no_manifest", "NO_MANIFEST"), ("no_reward", "OK"),
])
def test_partial_signal_requires_healthy_manifest_copies_and_reward(partial_trial, condition, expected):
    if condition == "failed":
        manifest(partial_trial, "failed")
    elif condition == "empty":
        shutil.rmtree(partial_trial / "artifacts/app/output")
    elif condition == "no_manifest":
        (partial_trial / "artifacts/manifest.json").unlink()
    else:
        data = json.loads((partial_trial / "result.json").read_text())
        data["verifier_result"] = None
        write_json(partial_trial / "result.json", data)
    (partial_trial / "verifier" / "test-stdout.txt").write_text(
        "FileNotFoundError: [Errno 2] No such file or directory: '/app/output/model-2.bin'\n"
    )
    assert row(partial_trial)["classification"] == expected


def test_partial_signal_is_reported_in_stdout_json_and_csv(partial_trial, tmp_path, capsys):
    line = "FileNotFoundError: [Errno 2] No such file or directory: '/app/output/model-2.bin'"
    (partial_trial / "verifier" / "test-stdout.txt").write_text(line + "\n")
    output, csv_output = tmp_path / "partial.json", tmp_path / "partial.csv"
    assert main([str(partial_trial), "--json", str(output), "--csv", str(csv_output)]) == 1
    assert "verifier/test-stdout.txt:1:" in capsys.readouterr().out
    assert line in json.loads(output.read_text())["trials"][0]["reason"]
    with csv_output.open(newline="") as stream:
        assert line in next(csv.DictReader(stream))["reason"]


def test_partial_signal_is_scoped_to_step_verifier_output(trial):
    data = json.loads((trial / "result.json").read_text())
    data["step_results"] = [{"step_name": "train", "verifier_result": {"rewards": {"reward": 0}}}]
    write_json(trial / "result.json", data)
    step = trial / "steps/train"
    manifest(step)
    payload(step)
    (step / "verifier").mkdir()
    (step / "verifier/test-stdout.txt").write_text(
        "FileNotFoundError: [Errno 2] No such file or directory: '/app/output/model-2.bin'\n"
    )
    result = row(trial)
    assert result["classification"] == "SUSPECT_PARTIAL_ARTIFACT"
    assert "steps/train/verifier/test-stdout.txt:1:" in result["reason"]


def test_partial_signal_does_not_read_symlinked_logs(partial_trial, tmp_path):
    outside = tmp_path / "external-log"
    outside.write_text("must not be read")
    (partial_trial / "verifier/test-stdout.txt").symlink_to(outside)
    assert row(partial_trial)["classification"] == "UNREADABLE"


def test_multi_step_artifacts_are_one_trial_row(trial):
    data = json.loads((trial / "result.json").read_text())
    data["step_results"] = [{
        "step_name": "train", "verifier_result": {"rewards": {"reward": 0}},
    }]
    write_json(trial / "result.json", data)
    step = trial / "steps" / "train"
    manifest(step, "failed")
    result = row(trial)
    assert result["classification"] == "GRADED_ON_FAILED_ARTIFACT"
    assert result["artifacts"][0]["step"] == "train"


@pytest.mark.parametrize("evidence", [None, "result", "verifier", "agent"])
def test_early_stop_requires_manifests_only_for_started_steps(trial, evidence):
    task = trial / "retained-task/task.toml"
    task.parent.mkdir()
    task.write_text(
        '[[steps]]\nname = "train"\nmin_reward = 1\n'
        '[[steps]]\nname = "evaluate"\nartifacts = ["/app/output"]\n'
    )
    data = json.loads((trial / "result.json").read_text())
    data["step_results"] = [{
        "step_name": "train", "verifier_result": {"rewards": {"reward": 0}},
    }]
    if evidence == "result":
        data["step_results"].append({"step_name": "evaluate"})
    elif evidence is not None:
        output = trial / "steps/evaluate" / evidence
        output.mkdir(parents=True)
        if evidence == "verifier":
            (output / "test-stdout.txt").write_text("Checking output\n")
    write_json(trial / "result.json", data)
    manifest(trial / "steps/train")
    payload(trial / "steps/train")
    result = row(trial)
    assert result["classification"] == ("NO_MANIFEST" if evidence else "OK")
    assert {item["step"] for item in result["artifacts"]} == (
        {"train", "evaluate"} if evidence else {"train"}
    )
    assert main([str(trial)]) == int(evidence is not None)


def test_unstarted_multi_step_trial_requires_no_manifest(trial):
    task = trial / "retained-task/task.toml"
    task.parent.mkdir()
    task.write_text('[[steps]]\nname = "train"\nartifacts = ["/app/output"]\n')
    data = json.loads((trial / "result.json").read_text())
    data["verifier_result"] = None
    data["step_results"] = []
    write_json(trial / "result.json", data)
    result = row(trial)
    assert result["classification"] == "NO_ARTIFACTS_DECLARED"
    assert result["artifacts"] == []
    assert main([str(trial)]) == 0


def test_single_step_root_manifest_survives_root_task_with_steps(trial, tmp_path):
    for name in ("config.json", "result.json"):
        data = json.loads((trial / name).read_text())
        data.get("config", data)["task"]["path"] = "tasks/foo"
        if name == "result.json":
            data["step_results"] = None
        write_json(trial / name, data)
    root = tmp_path / "task-root"
    task = root / "tasks/foo/task.toml"
    task.parent.mkdir(parents=True)
    task.write_text('[[steps]]\nname = "train"\nartifacts = ["/app/output"]\n')
    manifest(trial, "failed")
    result = audit([trial.parent, root])["trials"][0]
    assert result["classification"] == "GRADED_ON_FAILED_ARTIFACT"
    assert result["reward"] == {"reward": 0.0}
    assert result["artifacts"][0]["step"] is None
    assert result["artifacts"][0]["status"] == "failed"
    assert main([str(trial.parent), str(root)]) == 1


def test_step_only_rewards_are_visible_and_counted_as_zero(trial, capsys):
    data = json.loads((trial / "result.json").read_text())
    data["verifier_result"] = None
    data["step_results"] = [{
        "step_name": "train", "verifier_result": {"rewards": {"reward": 0}},
    }]
    write_json(trial / "result.json", data)
    manifest(trial / "steps" / "train", "failed")
    report = audit([trial])
    assert report["trials"][0]["reward"] is None
    assert report["summary"]["zero_reward_non_ok"]["GRADED_ON_FAILED_ARTIFACT"] == 1
    assert main([str(trial)]) == 1
    assert '"train": {"reward": 0}' in capsys.readouterr().out


@pytest.mark.parametrize("target", ["result.json", "artifacts/app/output", "artifacts/manifest.json"])
def test_does_not_follow_external_symlinks(trial, tmp_path, target):
    manifest(trial)
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("must not be read")
    link = trial / target
    if link.exists():
        link.unlink()
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(secret)
    assert audit([trial])["trials"][0]["classification"] == "UNREADABLE"
    assert secret.read_text() == "must not be read"


def test_symlinked_trial_is_not_discovered(trial, tmp_path):
    root = tmp_path / "scan"
    root.mkdir()
    (root / "external-trial").symlink_to(trial, target_is_directory=True)
    assert audit([root])["trials"] == []


def test_unparseable_default_named_job_does_not_hide_child_trials(trial):
    job = trial.parent.with_name("2026-10-27__12-34-56")
    trial.parent.rename(job)
    child = job / trial.name
    other = job / "checkpoint__def5678"
    shutil.copytree(child, other)
    for name in ("config.json", "result.json"):
        (job / name).write_text("{")
    paths = {item["trial_path"] for item in audit([job])["trials"]}
    assert {str(child), str(other)} <= paths


@pytest.mark.parametrize("marker", ["parsed", "trial.log"])
def test_confirmed_trial_prunes_artifact_payload_trials(trial, marker):
    if marker == "trial.log":
        for name in ("config.json", "result.json"):
            data = json.loads((trial / name).read_text())
            data.pop("trial_name", None)
            data.pop("task_name", None)
            write_json(trial / name, data)
        (trial / "trial.log").write_text("Starting trial\n")
    write_json(trial / "artifacts/nested__payload/result.json", {"task_name": "payload"})
    assert [item["trial_path"] for item in audit([trial])["trials"]] == [str(trial)]


def test_external_task_definition_is_not_opened(trial, tmp_path):
    task = tmp_path / "external-task"
    task.mkdir()
    (task / "task.toml").write_text("malformed TOML would fail if read")
    for name in ("result.json", "config.json"):
        data = json.loads((trial / name).read_text())
        config = data.get("config", data)
        config["artifacts"] = []
        config["task"]["path"] = str(task)
        write_json(trial / name, data)
    result = audit([trial])["trials"][0]
    assert result["classification"] == "NO_ARTIFACTS_DECLARED"
    assert any("outside scanned roots" in note for note in result["notes"])


def test_missing_result_is_unreadable(trial):
    (trial / "result.json").unlink()
    assert row(trial)["classification"] == "UNREADABLE"


def test_trial_log_without_either_metadata_file_is_unreadable(trial):
    damaged = trial.with_name("damaged-trial")
    trial.rename(damaged)
    for name in ("result.json", "config.json"):
        (damaged / name).unlink()
    (damaged / "trial.log").write_text("Starting trial\n")
    result = row(damaged)
    assert result["classification"] == "UNREADABLE"
    assert result["reason"] == f"missing file: {damaged / 'result.json'}"
    assert main([str(damaged)]) == 1


def test_read_only_scan_preserves_all_input_bytes(trial):
    manifest(trial)
    payload(trial)
    before = {str(p): p.read_bytes() for p in trial.parent.rglob("*") if p.is_file()}
    audit([trial.parent])
    after = {str(p): p.read_bytes() for p in trial.parent.rglob("*") if p.is_file()}
    assert after == before


def test_output_and_summary_and_exit_codes(trial, tmp_path, capsys):
    manifest(trial, "failed")
    out = tmp_path / "audit.json"
    csv_out = tmp_path / "audit.csv"
    assert main([str(trial.parent), "--json", str(out), "--csv", str(csv_out)]) == 1
    report = json.loads(out.read_text())
    assert report["summary"]["by_provider"]["modal"] == 1
    assert report["summary"]["by_classification"]["GRADED_ON_FAILED_ARTIFACT"] == 1
    with csv_out.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1
    assert rows[0]["classification"] == "GRADED_ON_FAILED_ARTIFACT"
    assert json.loads(rows[0]["artifacts"])[0]["status"] == "failed"
    assert "zero-reward" in capsys.readouterr().out
    manifest(trial)
    payload(trial)
    assert main([str(trial)]) == 0


def test_duplicate_overlapping_roots_deduplicate_trials(trial):
    manifest(trial)
    payload(trial)
    assert len(audit([trial.parent, trial, trial.parent])["trials"]) == 1


def test_cli_refuses_to_write_anything_under_scan_root(trial, capsys):
    result = trial / "result.json"
    before = result.read_bytes()
    with pytest.raises(SystemExit) as error:
        main([str(trial.parent), "--json", str(result)])
    assert error.value.code == 2
    assert result.read_bytes() == before
    assert "scanned" in capsys.readouterr().err


def test_output_replaces_external_hardlink_without_truncating_input(trial, tmp_path):
    manifest(trial)
    payload(trial)
    source = trial / "result.json"
    before = source.read_bytes()
    output = tmp_path / "linked-report.json"
    output.hardlink_to(source)
    assert main([str(trial), "--json", str(output)]) == 0
    assert source.read_bytes() == before
    assert "summary" in json.loads(output.read_text())


def test_usage_and_help(tmp_path, capsys):
    for argv in ([], [str(tmp_path / "missing")]):
        with pytest.raises(SystemExit) as error:
            main(argv)
        assert error.value.code == 2
    with pytest.raises(SystemExit) as error:
        main(["--help"])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "hash" in help_text
    assert "regrade" in help_text
    assert "SUSPECT_PARTIAL_ARTIFACT" in help_text
    assert "heuristic signal, not proof" in help_text
    assert "without such a message" in help_text


def test_real_receipt_job_structure_copied_to_temporary_fixture(tmp_path):
    # Metadata projection of the actual receipt job, see fixtures/README.md.
    source = Path(__file__).parent / "fixtures" / "receipt_job.json"
    job = tmp_path / "copied-job"
    for relative, data in json.loads(source.read_text()).items():
        write_json(job / relative, data)
    copy = tmp_path / "second-copy"
    shutil.copytree(job, copy)
    result = audit([copy])
    assert len(result["trials"]) == 1  # Job result is not a trial.
    item = result["trials"][0]
    assert item["classification"] == "NO_ARTIFACTS_DECLARED"
    assert item["reward"] == {"reward": 0.0}
    assert item["harbor_version"] == "0.24.0"
    assert item["provider"] == "tools.acceptance_receipt.compose_environment:ComposeEnvironment"
    assert item["notes"]  # Withheld artifacts are not an integrity proof.
