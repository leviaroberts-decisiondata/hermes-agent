# CLI and reviewer DGX component release

The release copies the two reviewed `ed06e69a39ebbc8c31d9f9243651a915416c1778`
component files into the old `~/.hermes/hermes-agent` CLI checkout:
`tools/transcription_tools.py` and `hermes_cli/config.py`. The component retains
the d1 runtime and history writer; its config change is the reviewed one-token
DGX default. Three existing profile configs change only their STT provider scalar:
`document-review`, `video-review`, and `security-review`.

Document Review remains available with its existing sessions. The helper never
edits launchers, gateway plists, the shared interpreter, histories or session data.
Source changes are guaranteed for **fresh CLI/background processes**. Existing
processes retain already-imported modules; config provider changes take effect on
the next config read. There is no claim of hot-updating existing jobs and no
authoritative cross-launch idle-state gate for these three CLI profiles.

## Interface

Run from an immutable tools clone containing the merged helper and dependencies:

```
python -B scripts/dd_hermes_cli_release.py stage --reviewed-commit FULL_MERGED_SHA
python -B scripts/dd_hermes_cli_release.py apply --manifest PRIVATE_PATH --manifest-sha256 SHA --queue-id auto
python -B scripts/dd_hermes_cli_release.py verify --manifest PRIVATE_PATH --manifest-sha256 SHA --queue-id UUID --challenge NONCE --attempt-id ATTEMPT
```

Stage reads the exact five allowed paths, their SHA256/mode/owner/group/mtime,
component provenance and helper hashes. It refuses unexpected baseline source,
other tracked source changes, symlinks or a gateway still using the old checkout.
The caller saves the non-secret manifest privately. Stage never reads credential
stores; private values are never included in output. Review pins the actual
helper commit, with component/default/history invariants still enforced.

Apply requires a matching approved/deploying MC row for `hermes-cli-dgx`, the exact
target and manifest-bound command. It takes one exclusive lock, consumes the queue
ID before mutation, saves private backups, and replaces only the five fixed files.
Each replacement checks the exact baseline immediately before atomic rename and
preserves ownership/group/mode. Rollback also restores the original mtime. A
protected gateway snapshot is captured at apply time under the lock; unrelated
changes during approval waiting do not invalidate the manifest. Its PID/cwd/plist
identities must remain unchanged through completion or recovery.

Failure allows one exact rollback, never overwriting a foreign file. The apply
budget is 25 seconds plus 10 seconds for rollback. Process/host death can leave a
partial transaction; its persisted operation fence requires operator recovery.
No external-supervisor crash guarantee is claimed.

Verify is a fresh read-only inspection. It binds the queue operation, target,
manifest and challenge; compares the actual five installed file hashes; and
imports the actual transcription/config modules from a neutral cwd through the
installed interpreter. Absolute console, PATH console alias and module resolution
must select the same CLI source. It resolves these entrypoints without executing
`main()`, which would write logs and start user flows. Subprocess guards reject
credential-store access, writes and network use even when imports catch refusals.
Canonical dotenv loading is retained, and optional dotenv identities are checked
only across the inspection, allowing unrelated token rotation while approval waits.
The inspection substitutes only `ensure_hermes_home`: it verifies existing
cron/session/log/memory directories and SOUL.md instead of chmod or seeding.
Actual config parsing, merging, environment expansion and transcription imports
remain unchanged. This is consumer import/config inspection, not full CLI startup.
Returned evidence includes actual module paths/digests, profile home and DGX
provider. Synthetic DGX network acceptance remains a separate attended check.

## MC and validation

Registration requires the separately reviewed explicit one-shot MC contract:
MC runs pinned apply argv once, then independently runs verify with a fresh
challenge/attempt identity. It validates the exact two-source/three-config map,
imported module identities and provider before its producer marks the release
proven. No persistent PID is invented or daemon proof downgraded; MC must not
retry apply as a restart. Every production queue approval belongs to Levi.

The component rail uses `scripts/run_dd_cli_release_tests.py --candidate PATH`.
It reuses the existing artifact harness, retaining mandatory attribution and P1
authorization checks plus ordinary/profile/component fixtures, and adds CLI
transaction fixtures. The full DD release gate also requires the CLI test file.
Author checks use `scripts/run_tests.sh tests/scripts/test_dd_hermes_cli_release.py`
with an isolated author venv; dependencies are never installed into the shared
serving interpreter. Fixtures exercise real subprocess console/PATH/module
launches using the actual d1 profile pre-parser/resolver and a report-only test
entrypoint, preserving cwd, `-p` selection and background `-z` arguments.
