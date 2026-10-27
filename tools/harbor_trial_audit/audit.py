"""Scan retained Harbor outputs without running Harbor or touching task data."""

import argparse
import csv
import io
import json
import math
import os
import posixpath
import re
import stat
import tempfile
import tomllib
from collections import Counter
from pathlib import Path, PurePosixPath


CLASSES = (
    "GRADED_ON_FAILED_ARTIFACT", "GRADED_ON_EMPTY_ARTIFACT",
    "SUSPECT_PARTIAL_ARTIFACT", "NO_MANIFEST",
    "OK", "NO_ARTIFACTS_DECLARED", "UNREADABLE",
)
HEALTHY = {"OK", "NO_ARTIFACTS_DECLARED"}
SUCCESS = {"ok", "success"}  # Harbor uses ok; success is also tolerated.
LIMITS = (
    "Presence and byte counts cannot prove intact bytes without source-side hashes. "
    "A present file can still be corrupt, including the GKE text-decoding failure "
    "in #3925. SUSPECT_PARTIAL_ARTIFACT is a heuristic signal, not proof: verifier "
    "output reports a missing file under a declared artifact destination despite "
    "a success manifest and nonempty copies. Partial loss without such a message "
    "stays undetectable without source-side hashes. Flagged grades need a human "
    "disposition: exclude, regrade from retained work, rerun, or keep as a genuine zero."
)
_LOG_PATH = r"""(?:'[^'\n]+'|"[^"\n]+"|[^\s'"]+)"""
_MISSING_PATHS = (
    re.compile(rf"(?:\bFileNotFoundError|\bNo such file or directory):\s*({_LOG_PATH})"),
    re.compile(rf"({_LOG_PATH}):\s*No such file or directory\b"),
)


def _within(path, roots):
    return any(path.is_relative_to(root) for root in roots)


def _safe(path, roots):
    """Refuse links, including intermediate components, before reading a path."""
    path = Path(os.path.abspath(path))
    boundary = next((root for root in roots if path.is_relative_to(root)), None)
    if boundary is None:
        raise ValueError(f"path outside scanned roots: {path}")
    current = boundary
    for part in ("", *path.relative_to(boundary).parts):
        current = current / part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"symlink not followed: {current}")
    return path


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _bad_constant(value):
    raise ValueError(f"non-finite JSON number: {value}")


def _json(path, roots, optional=False):
    path = _safe(path, roots)
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise ValueError("expected a regular file")
        with path.open(encoding="utf-8") as stream:
            data = json.load(stream, object_pairs_hook=_pairs, parse_constant=_bad_constant)
        if data is None:
            raise ValueError("expected an object or list, got null")
        return data
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError(f"missing file: {path}") from None
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"{path}: {error}") from error


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected an object")
    return value


def _rewards(result):
    verifier = result.get("verifier_result")
    if verifier is None:
        return None
    rewards = _object(verifier, "verifier_result").get("rewards")
    if rewards is None:
        return None
    _object(rewards, "verifier_result.rewards")
    for key, value in rewards.items():
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError(f"verifier_result.rewards[{key!r}]: expected a finite number")
    return rewards or None


def _declarations(config):
    values = config.get("artifacts", [])
    if not isinstance(values, list):
        raise ValueError("artifacts declaration must be a list")
    result = []
    for value in values:
        entry = {"source": value} if isinstance(value, str) else dict(
            _object(value, "artifact declaration")
        )
        _source(entry)
        if entry.get("destination") is not None:
            _relative(entry["destination"])
        result.append(entry)
    return result


def _source(entry):
    source = entry.get("source")
    if not isinstance(source, str) or not source or ".." in PurePosixPath(source).parts:
        raise ValueError("artifact source must be a non-empty path without '..'")
    service = entry.get("service")
    if service is not None and not isinstance(service, str):
        raise ValueError("artifact service must be a string or null")
    return source


def _relative(value):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("artifact destination must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe artifact destination: {value!r}")
    return Path(*path.parts)


def _key(entry):
    return (_source(entry).rstrip("/"), entry.get("service") or "main")


def _destination(entry, from_manifest=False):
    destination = entry.get("destination")
    if from_manifest and destination:
        relative = _relative(destination)
        if relative.parts[:1] != ("artifacts",):
            raise ValueError(f"manifest destination must begin with artifacts/: {destination}")
        return relative
    if destination:
        return Path("artifacts") / _relative(destination)
    return Path("artifacts") / _source(entry).lstrip("/")


def _convention(entry):
    return _key(entry) in {("/logs/artifacts", "main"), ("C:/logs/artifacts", "main")}


def _count(path, roots, manifest_path):
    path = _safe(path, roots)
    if path == manifest_path:
        return 0, 0
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return 0, 0
    if stat.S_ISREG(mode):
        return 1, path.stat().st_size
    if not stat.S_ISDIR(mode):
        raise ValueError(f"not a regular file or directory: {path}")
    count, size = 0, 0
    for child in sorted(path.iterdir()):
        child_count, child_size = _count(child, roots, manifest_path)
        count += child_count
        size += child_size
    return count, size


def _artifact_rows(base, declarations, roots, step=None):
    """A manifest is a JSON list, and its destinations include 'artifacts/'."""
    manifest = _json(base / "artifacts" / "manifest.json", roots, optional=True)
    if manifest is not None and not isinstance(manifest, list):
        raise ValueError(f"{base}/artifacts/manifest.json: expected a list")
    entries = []
    declared_keys = {(_key(entry), _destination(entry)) for entry in declarations}
    for entry in manifest or []:
        _object(entry, "manifest entry")
        _source(entry)
        if entry.get("status") is not None and not isinstance(entry["status"], str):
            raise ValueError("manifest status must be a string or null")
        if entry.get("type") not in (None, "file", "directory"):
            raise ValueError("manifest type must be file or directory")
        # An unused automatic publish directory is harmless; failed transfers are not.
        if (_convention(entry) and (_key(entry), _destination(entry, True)) not in declared_keys
                and entry.get("status") in SUCCESS | {"empty"}):
            continue
        entries.append(entry)
    matched = {(_key(entry), _destination(entry, True)) for entry in entries}
    entries += [entry for entry in declarations
                if (_key(entry), _destination(entry)) not in matched]
    rows = []
    for entry in entries:
        is_manifest_entry = any(entry is item for item in manifest or [])
        relative = _destination(entry, is_manifest_entry)
        count, size = _count(base / relative, roots, base / "artifacts" / "manifest.json")
        rows.append({
            "step": step, "source": entry["source"],
            "service": entry.get("service") or "main", "destination": relative.as_posix(),
            "status": entry.get("status"), "file_count": count, "bytes_present": size,
        })
    return rows, manifest is None


def _task_config(trial, configs, roots, notes):
    recorded_path = False
    for config in configs:
        task = _object(config.get("task", {}), "config.task")
        location = task.get("path")
        if location is None:
            continue
        if not isinstance(location, str):
            raise ValueError("config.task.path must be a string")
        recorded_path = True
        path = Path(location)
        candidates = [path] if path.is_absolute() else [base / path for base in (trial, *roots)]
        missing_notes = []
        for path in candidates:
            # Normalize /tmp's platform alias before checking scope, without following
            # arbitrary symlinks in the recorded task path.
            if str(path).startswith("/tmp/") and Path("/tmp").resolve() != Path("/tmp"):
                path = Path("/tmp").resolve() / path.relative_to("/tmp")
            path = Path(os.path.abspath(path / "task.toml"))
            if not _within(path, roots):
                missing_notes.append("Task definition outside scanned roots; declarations may be absent from retained records.")
                continue
            try:
                path = _safe(path, roots)
                if not stat.S_ISREG(path.stat().st_mode):
                    raise ValueError(f"task definition is not a regular file: {path}")
                with path.open("rb") as stream:
                    return tomllib.load(stream)
            except (FileNotFoundError, NotADirectoryError):
                missing_notes.append("Task definition not retained; declarations may be absent from retained records.")
        notes.extend(missing_notes)
    if not recorded_path:
        notes.append("task path not recorded for git/package task; declarations may be absent")
    return {}


def _partial_evidence(trial, artifacts, roots):
    """Match missing-file targets only in the corresponding verifier output."""
    for step in dict.fromkeys(item["step"] for item in artifacts):
        base = trial if step is None else trial / "steps" / step
        artifact_roots = {PurePosixPath("artifacts"), PurePosixPath(base / "artifacts")}
        destinations = []
        for item in artifacts:
            if item["step"] == step:
                # Harbor uploads collected copies to their original source path
                # inside the verifier environment, even for custom destinations.
                destinations.extend(PurePosixPath(value) for value in (
                    item["destination"], str(base / item["destination"]), item["source"],
                ))
        for name in ("test-stdout.txt", "test-stderr.txt"):
            path = _safe(base / "verifier" / name, roots)
            try:
                mode = path.stat().st_mode
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(mode):
                raise ValueError(f"expected a regular verifier output file: {path}")
            with path.open(encoding="utf-8", errors="replace") as stream:
                for number, line in enumerate(stream, 1):
                    for pattern in _MISSING_PATHS:
                        for match in pattern.finditer(line):
                            target = PurePosixPath(posixpath.normpath(match[1].strip("'\"").rstrip(":,;")))
                            if any(target.is_relative_to(dest) and (target != dest or dest not in artifact_roots)
                                   for dest in destinations):
                                return f"Heuristic signal, not proof: {path.relative_to(trial)}:{number}: {line.rstrip()}"
    return None


def _scan_trial(trial, roots):
    row = {
        "trial_path": str(trial), "task_name": None, "provider": None,
        "harbor_version": None, "reward": None, "step_rewards": {}, "artifacts": [],
        "classification": "UNREADABLE", "reason": "", "notes": [],
    }
    try:
        result = _object(_json(trial / "result.json", roots), "result.json")
        row["task_name"] = result.get("task_name")
        if row["task_name"] is not None and not isinstance(row["task_name"], str):
            row["task_name"] = None
            raise ValueError("task_name must be a string")
        row["reward"] = _rewards(result)
        embedded = _object(result.get("config", {}), "result.config")
        config = _json(trial / "config.json", roots, optional=True)
        config = _object(config, "config.json") if config is not None else {}
        configs = [embedded, config]
        # A standalone trial root need not grant access to its parent job.
        if _within(trial.parent, roots):
            job = _json(trial.parent / "config.json", roots, optional=True)
            if job is not None:
                configs.append(_object(job, "job config.json"))
        for location in (trial, trial.parent):
            if not _within(location, roots):
                continue
            lock = _json(location / "lock.json", roots, optional=True)
            if lock is not None:
                lock = _object(lock, "lock.json")
                harbor = _object(lock.get("harbor", {}), "lock.harbor")
                if harbor.get("version") is not None and not isinstance(harbor["version"], str):
                    raise ValueError("lock.harbor.version must be a string")
                row["harbor_version"] = row["harbor_version"] or harbor.get("version")
                if location == trial:
                    configs.append(lock)
        for item in configs:
            environment = _object(item.get("environment", {}), "environment")
            for key in ("type", "provider", "import_path"):
                if environment.get(key) is not None and not isinstance(environment[key], str):
                    raise ValueError(f"environment.{key} must be a string")
            row["provider"] = row["provider"] or (
                environment.get("type") or environment.get("provider") or environment.get("import_path")
            )
        task = _task_config(trial, configs, roots, row["notes"])
        declarations = []
        for item in [task, *configs]:
            for declaration in _declarations(item):
                if declaration not in declarations:
                    declarations.append(declaration)
        step_definitions = {}
        for step in task.get("steps") or []:
            step_definitions[_object(step, "task step")["name"]] = step
        steps = {}
        raw_step_results = result.get("step_results")
        step_results = raw_step_results or []
        if not isinstance(step_results, list):
            raise ValueError("step_results must be a list")
        for step in step_results:
            step = _object(step, "step result")
            name = step["step_name"]
            steps.setdefault(name, step_definitions.get(name, {}))
            row["step_rewards"][name] = _rewards(step)
        step_dir = _safe(trial / "steps", roots)
        if step_dir.exists():
            for directory in sorted(step_dir.iterdir()):
                _safe(directory, roots)
                if directory.is_dir():
                    steps.setdefault(directory.name, step_definitions.get(directory.name, {}))
        missing_manifest = False
        has_manifest = False
        scopes = steps.items() if steps or (step_definitions and raw_step_results is not None) else [(None, {})]
        for name, step in scopes:
            if name is not None and (not isinstance(name, str) or _relative(name).parts != (name,)):
                raise ValueError(f"invalid step name: {name!r}")
            base = trial if name is None else trial / "steps" / name
            artifacts, missing = _artifact_rows(base, declarations + _declarations(step), roots, name)
            row["artifacts"].extend(artifacts)
            missing_manifest |= missing and bool(artifacts)
            has_manifest |= not missing
        artifacts = row["artifacts"]
        failed = any(item["status"] is not None and item["status"] not in SUCCESS for item in artifacts)
        empty = any(item["bytes_present"] == 0 for item in artifacts)
        graded = bool(row["reward"]) or any(row["step_rewards"].values())
        if graded and not has_manifest:
            row["notes"].append("Graded trial has no artifact manifest in any scope; declarations may be absent from retained records.")
        row["notes"] = list(dict.fromkeys(row["notes"]))
        if not artifacts:
            classification, reason = "NO_ARTIFACTS_DECLARED", "No explicit artifacts in available declarations or manifests."
        elif graded and failed:
            classification, reason = "GRADED_ON_FAILED_ARTIFACT", "Verifier reward recorded despite a non-success artifact status."
        elif missing_manifest:
            classification, reason = "NO_MANIFEST", "Declared artifacts have no artifacts/manifest.json."
        elif graded and empty:
            classification, reason = "GRADED_ON_EMPTY_ARTIFACT", "Verifier reward recorded but a local artifact is missing or has zero bytes."
        elif failed or empty:
            classification, reason = "UNREADABLE", "Incomplete artifact evidence and no verifier reward; cannot classify as graded or OK."
        elif graded and (evidence := _partial_evidence(trial, artifacts, roots)):
            classification, reason = "SUSPECT_PARTIAL_ARTIFACT", evidence
        else:
            classification, reason = "OK", "All declared copies contain bytes; statuses are success or unspecified."
        row.update(classification=classification, reason=reason)
    except (OSError, ValueError, KeyError, TypeError, OverflowError, RecursionError) as error:
        row["reason"] = str(error)
    return row


def _discover(roots):
    trials = set()
    errors = []
    for root in roots:
        def onerror(error):
            errors.append({
                "trial_path": error.filename, "task_name": None, "provider": None,
                "harbor_version": None, "reward": None, "step_rewards": {}, "artifacts": [],
                "classification": "UNREADABLE", "reason": str(error), "notes": [],
            })
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
            path = Path(directory)
            dirs[:] = sorted(name for name in dirs if name != ".git" and not (path / name).is_symlink())
            if not {"result.json", "config.json", "trial.log"}.intersection(files):
                continue
            data = {}
            for filename in ("result.json", "config.json"):
                if filename in files:
                    try:
                        value = _json(path / filename, roots)
                        if isinstance(value, dict):
                            data.update(value)
                    except (OSError, ValueError, RecursionError):
                        pass  # A trial's diagnostic is produced by _scan_trial.
            is_job = "n_total_trials" in data or "job_name" in data or "tasks" in data
            is_trial = "trial_name" in data or "task_name" in data or "trial.log" in files
            if not is_job and (is_trial or "__" in path.name):
                trials.add(path)
                if is_trial:
                    dirs[:] = []  # Artifact payload result.json files are not trials.
    return sorted(trials), errors


def audit(paths):
    """Return trial rows and counts. Input roots must be existing directories."""
    roots = sorted({Path(path).resolve(strict=True) for path in paths})
    if not roots or any(not root.is_dir() for root in roots):
        raise ValueError("provide at least one existing directory")
    trials, errors = _discover(roots)
    rows = [_scan_trial(trial, roots) for trial in trials] + errors
    counts = Counter(row["classification"] for row in rows)
    zeros = Counter()
    for row in rows:
        rewards = [row["reward"]] if row["reward"] else [reward for reward in row["step_rewards"].values() if reward]
        if rewards and all(value == 0 for reward in rewards for value in reward.values()) and row["classification"] not in HEALTHY:
            zeros[row["classification"]] += 1
    return {
        "trials": rows,
        "summary": {
            "total_trials": len(rows),
            "by_classification": {name: counts[name] for name in CLASSES},
            "by_provider": dict(sorted(Counter(row["provider"] or "unknown" for row in rows).items())),
            "zero_reward_non_ok": {name: zeros[name] for name in CLASSES if name not in HEALTHY},
        },
        "limits": LIMITS,
    }


def _display(report):
    headings = ("TRIAL", "TASK", "PROVIDER", "HARBOR", "REWARD", "CLASSIFICATION", "ARTIFACTS (status, files, bytes)")
    print(" | ".join(headings))
    print("-" * 100)
    for row in report["trials"]:
        artifacts = "; ".join(
            f"{(item['step'] + ':') if item['step'] else ''}{item['source']} "
            f"[{item['service']}]: {item['status'] or 'unspecified'}, {item['file_count']} files, {item['bytes_present']} B"
            for item in row["artifacts"]
        ) or "none"
        values = [row["trial_path"], row["task_name"], row["provider"], row["harbor_version"],
                  json.dumps(row["reward"]) if row["reward"] else "none", row["classification"], artifacts]
        if row["step_rewards"]:
            values[4] += "; steps=" + json.dumps(row["step_rewards"])
        # Escape control characters from untrusted paths and metadata on stdout.
        print(" | ".join(str(value if value is not None else "unknown").encode("unicode_escape").decode("ascii") for value in values))
        if row["classification"] in {"UNREADABLE", "SUSPECT_PARTIAL_ARTIFACT"}:
            print(f"  Reason: {ascii(row['reason'])}")
        for note in row["notes"]:
            print(f"  Note: {note}")
    summary = report["summary"]
    print(f"\nTrials: {summary['total_trials']}")
    for label, key in (("Classification", "by_classification"), ("Provider", "by_provider"), ("Non-OK zero-reward", "zero_reward_non_ok")):
        print(f"{label}: " + ", ".join(f"{name}={count}" for name, count in summary[key].items()))
    print(f"\nLimits: {LIMITS}")


def _export(path, contents):
    """Replace an outside report atomically; never truncate a linked input file."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(contents)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Find Harbor trials graded on failed, empty, or suspect partial retained artifacts.",
        epilog=LIMITS + " NO_ARTIFACTS_DECLARED means none in available records, not proof that the original task declared none. "
        "No network calls. Symlinks within scanned trees are not followed. Exit: 0 healthy/no declarations, 1 any other class, 2 usage or output error.",
    )
    parser.add_argument("paths", nargs="+", metavar="PATH", type=Path)
    parser.add_argument("--json", metavar="OUT.json", type=Path, help="write JSON report outside scanned roots")
    parser.add_argument("--csv", metavar="OUT.csv", type=Path, help="write one CSV row per trial; artifacts encoded as JSON")
    args = parser.parse_args(argv)
    try:
        roots = [path.resolve(strict=True) for path in args.paths]
        outputs = []
        for output in (args.json, args.csv):
            if output is None:
                outputs.append(None)
                continue
            if output.is_symlink():
                raise ValueError(f"output cannot be a symlink: {output}")
            path = output.parent.resolve(strict=True) / output.name
            if _within(path, roots):
                raise ValueError(f"output must be outside scanned directories: {path}")
            outputs.append(path)
        if outputs[0] is not None and outputs[0] == outputs[1]:
            raise ValueError("JSON and CSV outputs must differ")
        report = audit(roots)
        if outputs[0] is not None:
            _export(outputs[0], json.dumps(report, indent=2, allow_nan=False) + "\n")
        if outputs[1] is not None:
            stream = io.StringIO(newline="")
            fields = list(report["trials"][0]) if report["trials"] else [
                "trial_path", "task_name", "provider", "harbor_version", "reward", "step_rewards",
                "artifacts", "classification", "reason", "notes",
            ]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for row in report["trials"]:
                writer.writerow({key: json.dumps(value) if isinstance(value, (list, dict)) else value for key, value in row.items()})
            _export(outputs[1], stream.getvalue())
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    _display(report)
    return int(any(row["classification"] not in HEALTHY for row in report["trials"]))
