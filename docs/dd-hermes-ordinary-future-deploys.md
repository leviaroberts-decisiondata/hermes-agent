# Ordinary Hermes after the September 7 cutover

The initial ordinary cutover is complete. At the September 8 housekeeping check,
ordinary serves from `/Users/openclaw/apps/hermes-agent-runtime-dgx-20260907`,
commit `9b75944b35ad2962c398a6215d5bc5aa1e86a518`, launchd label
`gui/502/ai.hermes.gateway`, port 8642. PID 31347 was the observed process, not a
permanent service identifier. Ordinary STT is already configured for DGX.

Future source releases use the existing MC source-promotion rail. The first-cutover
manifest is consumed and must not be reused. No new release dispatcher or transport
verifier is required for this ordinary contract. Classic is not included.

## Reviewed registry changes

Apply service discovery through the authoritative registry service, not its
generated JSON view and not MC's log-only `/api/services/register` endpoint:

```http
PATCH http://127.0.0.1:8500/services/hermes-agent
Content-Type: application/json

{"path":"/Users/openclaw/apps/hermes-agent-runtime-dgx-20260907"}
```

`PatchRequest` has no mandatory body fields. Path is the only necessary change;
port 8642, worker type, health URL and `ai.hermes.gateway` launch agent remain the
existing registration. Read back `GET /services/hermes-agent` and verify the path.
The registry itself persists its DB and refreshes its generated port-registry view.

In `deploy-commands.json`, update only `services.hermes-agent` with these fields:

```json
{
  "path": "/Users/openclaw/apps/hermes-agent-runtime-dgx-20260907",
  "repo": "/Users/openclaw/apps/hermes-agent-runtime-dgx-20260907",
  "type": "python",
  "build": null,
  "promote_source": true,
  "restart": "/bin/launchctl kickstart -k gui/502/ai.hermes.gateway",
  "test": "cd /Users/openclaw/apps/hermes-agent-runtime-dgx-20260907 && /Users/openclaw/.hermes/hermes-agent/venv/bin/python scripts/run_dd_release_tests.py",
  "_note": "Ordinary live source is /Users/openclaw/apps/hermes-agent-runtime-dgx-20260907, label ai.hermes.gateway, port8642; initial DGX cutover proven at9b75944b35ad2962c398a6215d5bc5aa1e86a518. Future approved deploys use MC buildless source promotion, exact-target isolated admission tests, source/release snapshot and MC-owned rollback/restart. Consumed first-cutover manifest is historical only. Generic runtime proof requires attended consumer acceptance; Classic unchanged. WTS7f86087b."
}
```

These are field updates, not a replacement service object. Preserve the complete
existing `build_rail` and all other registry fields. Its `test` remains the full
`scripts/run_dd_release_tests.py` gate, including mandatory attribution coverage.
The additional executor `test` runs the same gate immediately before promotion.
Apply reviewed registry changes with an archived original and a compare-and-swap
check; registration changes themselves do not execute a deploy or imply approval.

## Admission, mutation and rollback ownership

MC's independent admission-shadow path creates a worktree at the exact target SHA
for buildless services with `test` configured. It binds the registry command to
that shadow and rejects commands that escape the candidate. The absolute service
`cd` above is rewritten to the shadow. It does not run the test suite in the live
serving tree. The runner uses the existing interpreter without installing packages,
isolates Hermes home and removes inherited provider credentials before collection.

For `build:null`, `type:python`, `promote_source:true`, MC resolves the target,
checks live-ahead drift, acquires its repository mutation lease, and snapshots the
actual prior tracked source plus the exact prior release marker before mutation.
Incomplete snapshots fail before promotion. MC then promotes tracked source and
stamps the target release marker; untracked runtime configuration is outside this
source operation. It does not move the serving branch HEAD.

The ordinary `kickstart -k` runs only as the restart command of a Levi-approved queue
operation. It can interrupt active work: this is a normal brief-maintenance release.
After restart, MC checks the expected process/cwd, commit evidence, configured health
and crash-loop signals. A post-mutation failure enters MC's single finalizer, which
restores the source/release snapshot and, if restart may have changed the process,
restarts and re-proves the prior runtime. Missing recovery evidence remains failure.
The historical cutover helper is not a second rollback owner on this path.

The rollback baseline is the immediately previous known-good source snapshot.
Do not substitute the archived original ordinary `d1a294da` runtime: that old reader
predates structured history decoding and is not a lossless rollback target for new
history. Current ordinary `9b75944b` includes the decoder. Future changes to stored
history still need explicit old/new reader compatibility tests before submission.

Fetch the merged target ref without rewriting the serving tree, prove that exact
merge through the existing build rail, submit a fresh queue row under WTS 7f86087b,
and leave approval to Levi. Never execute the restart command manually as a test.

## What the normal proof does not establish

MC's generic buildless proof is not the first-cutover helper's enabled-transport
stability check, a DGX inference proof, or a user-visible Telegram round trip.
Hermes currently has no process-originated `/api/release` endpoint. Do not enable
or disable identity-enforcement settings to manufacture a pass; an enforced missing
contract must be addressed separately. With the current standard rail, retain an
attended transcription/message acceptance check after an approved release and record
any unproven result honestly. Neither this housekeeping nor its offline review
performs those consumer actions.

## Existing executor evidence

This contract follows the installed MC implementation, rather than proposing a new
shared rail: `_needs_independent_pretest_shadow` and `_run_registry_pre_test` bind
admission to the target; `_service_requires_source_promote` selects buildless opt-in;
the `is_source_promote_only` branch snapshots before promotion; and
`_fail_after_mutation` owns source restoration and prior-runtime re-proof. Service
discovery's `routes/services.py:patch_service` updates only specified fields and
calls `writeback` after committing the authoritative registry DB.
