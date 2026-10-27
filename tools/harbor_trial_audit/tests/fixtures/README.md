# Receipt job fixture

`receipt_job.json` is a metadata projection generated from the read-only job at:

```text
/tmp/nsp-cpp/receipts/driver-B/items/ncpp-final-driver-b/null-attempt-1/job/
```

The trial is `task__mXTUHZL`, task `terminal-bench/freight-dispatch-shift`, reward
`0.0`, with Harbor `0.24.0` and source revision
`4b59c5c46239e92f6c9fde772bdc1a70bdaead02` in the job lock. The recorded environment
is `tools.acceptance_receipt.compose_environment:ComposeEnvironment`.

The fixture retains the actual job and trial configs and the trial result's
`task_name`, `trial_name`, `config`, `verifier_result`, and `exception_info`.
It projects the job result to its trial count and completion count, and the job
lock to its schema and Harbor metadata. Verifier diagnostics and logs are omitted
to keep the fixture small. No artifact manifest exists in this public receipt;
acceptance receipts withhold the artifact subtree and the original task path is
not retained here.

The test materializes these files, copies the resulting job to a temporary
directory, and scans the copy. It asserts one trial, not the job summary, and
`NO_ARTIFACTS_DECLARED` with a note about missing task declarations. This is a
layout and metadata regression, not a claim of artifact integrity.

`tests/test_audit.py` also generates the synthetic `partial_trial` fixture in a
temporary directory. It declares `/app/output`, writes a successful manifest,
and retains only `solve.py` and `model-1.bin`, with recorded reward zero. The
positive adds a verifier `FileNotFoundError` for missing `model-2.bin` inside that
destination. Negative variants name a missing file outside the destination or
provide no missing-file error. The latter stays `OK`: without a source inventory
or hashes, the partial set alone cannot prove loss. These are heuristic tests,
not evidence that transport caused any historical grade.
