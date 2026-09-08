# Ordinary Hermes first cutover

`scripts/dd_hermes_ordinary_release.py` implements an **ordinary-only, queue-approved
brief maintenance cutover**. It is uninstalled until the helper's final merged
commit has been proven, a candidate prepared, and its queue row approved. It never
manages Classic or another gateway label. It does not claim Classic's external
supervisor recovery contract.

The helper preserves the original runtime directory and switches only ordinary's
LaunchAgent to an isolated candidate under `/Users/openclaw/apps`. A shared,
read-only Python executable is allowed: its SHA256 is pinned, and a bounded
subprocess checks that `run_agent` and `hermes_cli` resolve from the candidate cwd.
Only ordinary's `~/.hermes/config.yaml` is changed: `stt.provider` becomes `dgx`.
The YAML scalar edit preserves other values and comments and is parsed again to
prove that no other configuration changed. Other profiles remain untouched.

## Read-only staging interface

Prepare an inactive clean clone with owner-fork `origin`, the exact final merged
target, and a provisioned Python environment. Do not point it at the upstream
Nous origin: containment and future proof must use the reconciled owner fork.

```sh
PYTHON=/Users/openclaw/.hermes/hermes-agent/venv/bin/python
CANDIDATE=/Users/openclaw/apps/hermes-agent-runtime-dgx-20260907
"$PYTHON" "$CANDIDATE/scripts/dd_hermes_ordinary_release.py" stage \
  --candidate "$CANDIDATE" --python "$PYTHON" --target MERGED_40_HEX_SHA
```

`stage` reads source identity, ordinary's installed plist, loaded PID/cwd, fresh
process-owned HTTP health and idle count. It emits a non-secret JSON manifest to
stdout and writes nothing. The caller saves those exact bytes in an operator-owned
file outside the candidate and calculates their SHA256. Do not include unrelated
shell output in that file.

The manifest binds schema/service/label, exact target/source tree, candidate path,
Python path/hash, baseline PID/cwd, baseline source signature/interpreter hash,
baseline and candidate plist hashes, baseline ordinary config/environment hashes,
candidate config hash, baseline-connected transports, 15s activation/10s recovery
budgets, and the disclosed idle-check race. Baseline `updated_at` is a transition
timestamp, not a heartbeat: fresh HTTP and matching loaded PID establish baseline
liveness. After restart, new PID/cwd and fresh process/transport timestamps are
required. All baseline-connected transports must recover, including Feishu when
present; an already-disconnected transport is not newly required.

## Exact queue binding

Call `restart_command(manifest_path, manifest_sha256)` from the **candidate's**
helper module using the provisioned interpreter to generate the exact command.
The resulting command is equivalent to:

```text
/Users/openclaw/.hermes/hermes-agent/venv/bin/python /Users/openclaw/apps/hermes-agent-runtime-dgx-20260907/scripts/dd_hermes_ordinary_release.py activate --manifest /ABSOLUTE/MANIFEST.json --manifest-sha256 FILE_SHA256 --queue-id auto --label gui/502/ai.hermes.gateway
```

Verify it fits the queue's 500-character command limit. The helper does not assume
MC exports a queue ID. `auto` selects exactly one **deploying** `hermes-agent` row
whose target and entire restart-command string match, then fetches that ID again.
The fetched row must be approved/deploying with `decided_by`, `decided_at`, matching
service/target/command, and a UUID ID. It rechecks approval immediately before stop.
An explicit `--queue-id UUID` supports inspection/testing of an already-bound row;
the approved restart command remains the canonical `--queue-id auto` command.
Matching an approved row is an execution guard, not a new approval mechanism.

Prospective `deploy-commands.json` slot (registration is a separate authorized act):

```json
{
  "type": "python",
  "path": "/Users/openclaw/apps/hermes-agent-runtime-dgx-20260907",
  "repo": "/Users/openclaw/apps/hermes-agent-runtime-dgx-20260907",
  "port": 8642,
  "build": null,
  "promote_source": false,
  "restart": "EXACT restart_command OUTPUT",
  "probe_paths": ["/health"],
  "test": null,
  "build_rail": {
    "test": "cd /Users/openclaw/apps/hermes-agent-runtime-dgx-20260907 && /Users/openclaw/.hermes/hermes-agent/venv/bin/python scripts/run_dd_release_tests.py"
  }
}
```

Retain the existing valid build/lint/type-check rail entries and adjust their cwd
to the candidate. Add the helper's fixture test to the mandatory release test list
before proving its merged target. Do not invent a build proof or release marker.
The helper's own stdout is operational evidence, not a build-rail proof.
The existing null executor test slot is preserved: the full regression suite is
proven before submission, and activation rechecks the manifest, source, baseline,
approval and idle state immediately before its own mutation.

For this first cutover, MC must **not** promote source or run a mutating build.
The helper owns the plist/config transaction. MC then verifies the returned runtime
through its ordinary port and realization checks. Update service discovery through
the registry service when onboarding, not by hand-editing its generated port JSON.

## Transaction and limitations

Activation takes an exclusive nonblocking lock under
`~/.openclaw/hermes-ordinary-release` (0700) and persists the consumed queue ID before
service mutation. Baseline plist/config backups are private 0600 files. Mutation
intents are journaled before stop, replacement and start. It sends one named
`launchctl bootout`, waits for the old PID to disappear, writes config/plist, and
bootstraps only ordinary. The candidate launcher omits `--replace`, so it cannot
take over another surviving process.

Success requires a changed PID at the candidate cwd, process-owned fresh health,
all enabled baseline-connected transports and a one-second stable observation window,
followed by source/interpreter/config revalidation. This is **not** model-quality,
DGX transcription execution, or a Telegram round-trip proof. A human-visible audio
canary remains an attended post-deploy acceptance step; the helper sends no messages.

Failure after stop triggers at most one exact baseline rollback, bounded by an
absolute 25-second overall deadline. The helper restores baseline bytes only if
current plist/config still matches either its baseline or candidate hashes. It
refuses to overwrite another writer's changes or stop a foreign cwd. Rollback
requires a fresh baseline process and the same transport/stability checks.
Uncertain stop with an old PID still alive must never bootstrap over that PID.

Each subprocess/HTTP call is bounded by the remaining deadline and a two-second
cap. There is no repeated kill loop. Filesystem syscalls still assume responsive
local storage; the budgets are not a hard real-time guarantee. MC's command timeout
is 30s, so a failure to finish must not be presented as a completed cutover.

The idle check reduces interruption risk but cannot exclude a new turn racing it.
Normal service-manager shutdown may interrupt that turn. Levi approves the disclosed
brief maintenance interruption through the concrete queue row.

Recovery is **in-process only**. If this helper, its host process, or the machine
dies after mutation, no automatic restoration is claimed. Its unresolved journal
blocks new operations; a repeated consumed queue ID always refuses. An operator
must inspect the private snapshots, actual label/PID/cwd and current bytes before
manual recovery. This deliberately does not import or invoke the retired Classic
activator or expand Classic's approval contract.

## Persisted transport state and recovery acknowledgment

Detailed health can retain connected entries from an older process. Stage resolves
the baseline's enabled transports through its own canonical dotenv/config loader,
using the installed plist environment. It requires the candidate to resolve the
same enabled set. Required transports are the connected baseline entries in that
enabled set; disabled persisted entries are recorded as `ignored_disabled_platforms`.
Telegram and API remain mandatory, and enabled extra transports remain required.
This does not infer process start time from status-transition timestamps.

The resolver runs with bytecode writes disabled and an audit guard installed before
gateway imports. Writes, credential-store opens, networking and subprocess launches
refuse, even if an optional loader catches the refusal. Existing-directory mkdir
requests return FileExistsError without an OS write so canonical `exist_ok=True`
calls can proceed; creation of a missing directory still refuses. Chmod requests
also return without an OS write when the actual mode already matches; any permission
change still refuses. Older baselines
without the plugin registry are supported. Enabled plugin transports refuse because
their external configuration is outside this manifest; disabled discovered plugins
do not become requirements. Only enabled names or fixed failure codes leave the
resolver. Home config, home `.env`, legacy `gateway.json`, and both project `.env`
files are pinned, including their absence.

Socket construction is allowed, but DNS/connect/bind/send operations remain blocked.
The inspection subprocess sets its own `socket.has_ipv6=False` to skip urllib3's
import-time localhost bind capability probe. This changes no runtime configuration,
service environment or service networking.

After independently restoring ordinary, an operator can acknowledge a
`failed_recovery` journal using its exact prior operation and state digest:

```sh
python scripts/dd_hermes_ordinary_release.py acknowledge-recovery \
  --manifest /absolute/path/to/old-manifest.json \
  --manifest-sha256 OLD_MANIFEST_SHA256 \
  --expected-operation FAILED_QUEUE_UUID \
  --expected-state-sha256 CURRENT_FAILED_STATE_SHA256
```

Keep the old candidate unchanged until acknowledgment completes. The command checks
the failed terminal queue row and original manifest binding; immutable candidate;
original baseline source, interpreter, config and plist; and actual baseline PID,
cwd, health and required enabled transports. It writes no runtime or queue data.
Under the existing exclusive lock it archives the exact failed state privately and
appends `operator_recovered` evidence, preserving old failure events and consumed IDs.
An immutable private recovery receipt retains that evidence after a later operation.
The old deploy remains failed. A new activation needs a new manifest and a different
approved queue operation; acknowledgment never permits replay of the failed one.

Activation now records safe `failure_reason` and `recovery_failure_reason` codes,
including the last failed transport probe when verification expires. It never emits
raw subprocess stderr, configuration values or credentials.

## Fixture validation

```sh
/Users/openclaw/.hermes/hermes-agent/venv/bin/python -m pytest \
  -o addopts= -n 4 tests/scripts/test_dd_hermes_ordinary_release.py -q
```

These tests mock OS/HTTP interactions and write only temporary fixture files. They
cover queue admission, idle/source/import refusal, successful config/plist switch,
all connected transports, one rollback, private exact backups, concurrent/replayed
operations, foreign plist preservation and uncertain stop with an old PID alive.
The normal repository wrapper may install missing pytest-split into a borrowed
serving environment; use this documented no-install fallback when necessary.
