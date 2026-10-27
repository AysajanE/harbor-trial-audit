# harbor-trial-audit

A dependency-free Python 3.11+ scanner for retained Harbor job and trial results.
It finds grades recorded despite failed artifact collection, missing local
copies, or verifier messages suggesting partial copies. It reads existing files,
makes no network calls, and does not run Harbor,
verifiers, containers, or tasks.

Bespoke Labs reported that Modal downloads over 5 GiB could fail while a separate
verifier still recorded an ordinary zero reward in
[#3924](https://github.com/harbor-framework/harbor/issues/3924). GKE transfers could
also corrupt binary data or silently lose later files in
[#3925](https://github.com/harbor-framework/harbor/issues/3925).
The transport fixes proposed in
[#3927](https://github.com/harbor-framework/harbor/pull/3927) and
[#3928](https://github.com/harbor-framework/harbor/pull/3928) do not audit historical
grades. This tool identifies retained evidence that needs a human decision.

## Run it

From the repository root:

```sh
python3 -m tools.harbor_trial_audit /data/jobs /data/other-job \
  --json /reports/audit.json --csv /reports/audit.csv
```

Each path must be an existing directory. Scans are recursive, accept receipt
packages, job directories, or individual trials, and deduplicate overlapping roots.
There is one row per trial, including trials with step-level artifacts. Job
summary `result.json` files and JSON files inside artifact payloads are not trials.

The table shows the trial path, task name, recorded environment type or provider
(or custom environment import path), recorded Harbor version, reward dimensions,
and each declared artifact's status, regular-file count, and total logical bytes.
Unknown metadata stays unknown. Missing rewards stay `none`, never zero.
For example:

```text
TRIAL | TASK | PROVIDER | HARBOR | REWARD | CLASSIFICATION | ARTIFACTS (status, files, bytes)
/jobs/run/checkpoint__abc1234 | checkpoint | modal | 0.24.0 | {"reward": 0.0} | GRADED_ON_FAILED_ARTIFACT | /app/output [main]: failed, 0 files, 0 B

Trials: 1
Classification: GRADED_ON_FAILED_ARTIFACT=1, GRADED_ON_EMPTY_ARTIFACT=0, SUSPECT_PARTIAL_ARTIFACT=0, NO_MANIFEST=0, OK=0, NO_ARTIFACTS_DECLARED=0, UNREADABLE=0
Provider: modal=1
Non-OK zero-reward: GRADED_ON_FAILED_ARTIFACT=1, GRADED_ON_EMPTY_ARTIFACT=0, SUSPECT_PARTIAL_ARTIFACT=0, NO_MANIFEST=0, UNREADABLE=0
```

JSON includes the rows, classification/provider counts, zero-reward counts, and
limits. CSV also has one row per trial, with `reward`, `step_rewards`, `artifacts`,
and `notes` stored as JSON cells. Zero-reward counts require a nonempty trial
reward map whose recorded dimensions are all zero, or all-zero recorded step
rewards when the trial map is absent. No declaration counts as an
informational result, so it is excluded from the non-OK zero-reward counts.

Exit status is `0` for only `OK` or `NO_ARTIFACTS_DECLARED` results, including an
empty directory; `1` if any other class occurs; `2` for usage or output errors.
Reports must be outside the scanned roots, with existing parent directories.
Existing reports are replaced atomically. Symlink report destinations are refused.

## Classes and action

| Class | Meaning | Operator action |
| --- | --- | --- |
| `GRADED_ON_FAILED_ARTIFACT` | A reward exists and a declared artifact has a non-success status, including Harbor's `failed`, `empty`, or `skipped`. | Review collection evidence. Exclude, regrade retained work, rerun, or keep as a genuine zero after investigation. |
| `GRADED_ON_EMPTY_ARTIFACT` | A reward exists; status is `ok`, `success`, or unspecified, but a declared copy is missing or contains zero bytes. | Check whether empty output was intended, then choose a disposition. |
| `SUSPECT_PARTIAL_ARTIFACT` | A reward exists; every declared copy contains bytes and manifest statuses are success or unspecified, but verifier output reports a missing path at or under a declared artifact destination. The reason reports the matched file, line number, and line. This is a heuristic signal, not proof. | Compare retained work with source-side hashes before deciding whether to exclude, regrade, rerun, or keep the grade. |
| `NO_MANIFEST` | Available records declare artifacts but a required `artifacts/manifest.json` is missing. | Recover the original manifest and outputs, or exclude/rerun when evidence is insufficient. |
| `OK` | Declared copies contain bytes, statuses are success or unspecified, and no partial-copy signal was found. | No structural finding. Check source hashes separately if integrity matters. |
| `NO_ARTIFACTS_DECLARED` | No explicit declarations or manifest entries apply to the audited scopes. | Confirm task definitions were retained before interpreting this as evidence of no artifact requirement. |
| `UNREADABLE` | Metadata is malformed, a required result cannot be read, a path is unsafe, or artifact evidence is incomplete without any recorded reward. The row gives a reason. | Recover evidence and rescan; do not infer a genuine zero. |

If several conditions occur, unreadable metadata takes precedence, followed by
graded failures, missing manifests, graded empty copies, and suspect partial
copies. The suspect class ranks after both `GRADED_ON_*` classes and before `OK`.
Every artifact row remains available for inspection. A `GRADED_*` label describes recorded evidence;
it does not prove that transport caused the grade. It can flag an intentionally
empty output or an agent failure that deserves its recorded zero.

## What is read

The layout was checked against Harbor main at
[`e77a18bfeed53f907f335b9132d87006e01b3dfd`](https://github.com/harbor-framework/harbor/tree/e77a18bfeed53f907f335b9132d87006e01b3dfd)
and
[`4b59c5c46239e92f6c9fde772bdc1a70bdaead02`](https://github.com/harbor-framework/harbor/tree/4b59c5c46239e92f6c9fde772bdc1a70bdaead02).
Relevant source files are `models/trial/{paths,result,artifact_manifest}.py`,
`models/job/lock.py`, `models/task/{config,artifacts}.py`, and
`trial/{artifact_handler,multi_step}.py`, `verifier/verifier.py`, and
`utils/scripts.py`, under `src/harbor/`.

- A job holds trial directories beside its own `config.json`, `result.json`,
  and `lock.json`. Trial results embed runtime config and
  `verifier_result.rewards`. Multi-step results also record `step_results`.
  A retained `trial.log` identifies a trial even when both JSON files are absent;
  the missing required `result.json` produces `UNREADABLE` and exit status `1`.
- Artifact declarations come from trial config, job config, a referenced
  `task.toml` when accessible inside the given roots, and manifest entries.
  Task-level and step-level declarations are merged with runtime declarations.
  A relative task path is tried against the trial directory, then each scanned
  root as a base, using the first retained task definition inside the roots.
  Only steps with a recorded step result or a retained `steps/<name>/` output
  directory are audited. Harbor creates these directories when a step starts;
  task definitions alone do not establish execution. Later steps skipped after
  an early stop do not require manifests, while started steps still do.
- `artifacts/manifest.json` is a JSON list. Its entries record `source`,
  `destination`, `type`, `status`, and optionally `service` and `exclude`.
  Manifest destinations start with `artifacts/`. An explicit declaration's
  destination is relative to that directory; otherwise the absolute source path
  is mirrored below it. Step outputs live at `steps/<name>/artifacts/`.
  Declarations match manifest entries by source, service, and normalized effective
  destination. A collected copy cannot hide another missing destination for the
  same source and service.
- Harbor implicitly collects `/logs/artifacts` (or `C:/logs/artifacts`). This
  convention's entries with status `ok`, `success`, or `empty` are ignored unless
  explicitly declared, so an unused directory (`ok` with zero files, or `empty`)
  does not flag. Entries with other statuses, including `failed` and `skipped`,
  are retained and classified like declared artifacts. Harbor uses status `ok`;
  `success` is also tolerated. A missing status is accepted when bytes exist.
- `_download_artifact` catches transfer exceptions, logs them at DEBUG, and
  returns a manifest entry with `status="failed"`. It can leave an empty target
  directory. This is the evidence behind the first classification.
- At both revisions, `TrialPaths.test_stdout_path` is
  `verifier/test-stdout.txt`, and `test_stderr_path` is
  `verifier/test-stderr.txt`. `Verifier.verify` uses `build_execution_command`,
  which redirects stdout **and stderr** to `test-stdout.txt` with `2>&1`.
  The scanner checks both named files when retained, under
  `steps/<name>/verifier/` for step-level artifacts. It does not search agent logs
  or the general lifecycle log `trial.log` for verifier failures.
- The partial-copy heuristic recognizes `FileNotFoundError: <path>`, Python's
  `No such file or directory: <path>`, and shell-style
  `<path>: No such file or directory` messages. Paths must equal or be descendants
  of that scope's declared destinations, either relative to the scope or absolute
  on the host. Exact declared file and directory paths match too; a message naming
  only the bare `artifacts` root does not match. Harbor's `upload_artifacts`
  restores each copy to its original `source`
  path inside the verifier environment, so those upload destinations also match,
  including when the host destination is customized. Sibling paths and paths
  escaping the destination with `..` do not match. The report's `reason` contains
  the log path relative to the trial, line number, and full matched line, including
  in JSON and CSV. Only the first match is reported.
- Environment metadata comes from recorded `environment` config. Harbor version
  comes from `lock.json`'s `harbor.version`, usually in the parent job. A scan of
  an individual trial does not read parent metadata outside the provided roots.

## Limits

The scanner **cannot prove that bytes are intact without source-side hashes**.
A present file can still be corrupt, including the GKE text-decoding failure in
#3925. Harbor's manifests at the checked revisions contain no per-file inventory,
expected sizes, or source hashes. `SUSPECT_PARTIAL_ARTIFACT` is a **heuristic
signal, not proof**: an agent could simply have failed to create the named file.
A partial loss without such a message stays undetectable without source-side
hashes, and a partial file set with some bytes present can still be `OK`.
Missing, filtered, or differently formatted verifier output also limits the
heuristic. There is no defensible expected-file-count check. The tests explicitly
preserve that limitation.

Missing task definitions also limit the scan. `config.artifacts` contains runtime
additions, not the full task definition, and trial locks do not retain task
artifact declarations. If a task definition is missing or outside the provided
roots, a note says declarations may be absent from retained records.
When no config records a task path, the note is
"task path not recorded for git/package task; declarations may be absent".
A graded trial with no artifact manifest in any scope also receives a note.
With no manifest, visible artifact declarations in an audited scope produce
`NO_MANIFEST`; when no declaration is visible, the result remains
`NO_ARTIFACTS_DECLARED` with those notes.
`NO_ARTIFACTS_DECLARED` then means only that nothing is visible to check.
Provide the retained task directory as another input root when its recorded
path still resolves to that location.

Acceptance receipt packages deliberately withhold artifact and agent subtrees.
The healthy local Docker receipt examples therefore exercise discovery and
metadata handling, not artifact integrity. Scanning these packages cannot
establish that their original artifacts arrived intact.

Scans never follow symlinks inside input trees, even links within a root.
Symlinked directories are skipped during discovery; symlinks in a trial's
referenced metadata or declared copies produce `UNREADABLE`. Input paths are
resolved to establish scan boundaries. Recorded task paths outside those
boundaries are not opened. Counts exclude the collection manifest itself.
Scan completed, stable directories; this tool does not take an atomic snapshot
of concurrently changing files.

No exclusion, regrade, rerun, or reward change is automatic. A person must choose
the disposition and retain the evidence for that choice.

## Tests

```sh
PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python3.11 -m pytest -q tools/harbor_trial_audit -p no:cacheprovider
```

Tests use synthetic Harbor layouts and a small metadata projection of a real
receipt job. There are no runtime dependencies beyond the Python standard library;
pytest is used only for development tests.
