# harbor-trial-audit

A small, dependency-free Python 3.11+ scanner for retained [Harbor](https://github.com/harbor-framework/harbor) job and trial results.
It lists every trial that recorded a reward while a declared artifact failed to collect, arrived empty, or looks partially copied, with counts per provider.
It reads existing files only. It makes no network calls and runs no containers, verifiers, or tasks.

```sh
python3 -m tools.harbor_trial_audit /path/to/jobs --json audit.json --csv audit.csv
```

Run it from the repository root. Full usage, classifications, exit codes, and limits are in [tools/harbor_trial_audit/README.md](tools/harbor_trial_audit/README.md).

Development tests:

```sh
python3.11 -m pytest -q tools/harbor_trial_audit -p no:cacheprovider
```

Licensed under the Apache License 2.0. Maintained by Northset (https://northset.ai).
