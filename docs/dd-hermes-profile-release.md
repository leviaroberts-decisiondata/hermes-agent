# DGX component releases for 15 additional Hermes profiles

This is a narrow component release on the existing d1 runtime, not fleet convergence
to current main. It does not change history writers, inference models, agent
configuration, ordinary or Classic. Each activation requires Levi's approval of its
own exact queue operation and discloses a brief maintenance interruption.

## Candidate and reviewed source

Initial prepared artifact (not activated):

- Path: `/Users/openclaw/apps/hermes-profile-runtime-dgx-20260908`
- Branch: `release/profile-dgx-d1-20260908`
- Commit: `ed06e69a39ebbc8c31d9f9243651a915416c1778`
- Tree: `a8d1f1b27ce0884794c236406316ab8ab87aa192`
- Base: `d1a294da78c45a18c9f8cc89fa992e33b4cc0282`

Only two runtime files change. `tools/transcription_tools.py` is byte-identical to
the reviewed merged source. `hermes_cli/config.py` preserves every d1 byte except
the quoted `DEFAULT_CONFIG.stt.provider` value `local` → `dgx`; its new default is
also verified against reviewed main. The original `hermes_state.py` blob remains
identical. The manifest records base, artifact commit/tree, reviewed commit, exact
transcription blob and all three default-config blobs. This avoids introducing a
new persisted history format or requiring a reader-foundation production change.

`component_identity` rejects extra changed files, a different transcription blob,
any other config-byte edit, a changed history writer, or a reviewed commit not
contained in the fetched `origin/main`. It does not misrepresent the composite
artifact as the full main tree. MC source promotion is deliberately disabled:
the helper switches the target profile to an immutable artifact instead.

The d1 Telegram adapter caches incoming audio, and its gateway transcription method
does not delete that cache on STT failure. The fixture executes the actual d1 method
with a failed transcription and verifies that the recording remains. The separate
interactive CLI cleanup changes are not needed for these running Telegram consumers
and are not silently included in this component release.

## Helper and proof source

After merging the helper PR, use a separate immutable tools clone under a short
path such as `/Users/openclaw/apps/hpr-<merge-sha12>`. It contains the main helper,
ordinary transaction engine, both proof drivers and their fixtures. Stage requires
their exact bytes to match the named reviewed commit and pins their SHA256 digests.
Do not place those files inside the two-file runtime artifact or execute a mutable
author checkout after approval. Fetch the reviewed main ref in the artifact repo.

The component proof command is:

```sh
cd /Users/openclaw/apps/hermes-profile-runtime-dgx-20260908 && \
  /Users/openclaw/.hermes/hermes-agent/venv/bin/python \
  /Users/openclaw/apps/hpr-MERGESHA12/scripts/run_dd_profile_release_tests.py --candidate .
```

Use this in both the new profile service's executor `test` and `build_rail.test`.
MC binds `cd` to its exact-target admission shadow; `--candidate .` then names that
shadow, not the live source. The external driver remains pinned independently.
It first runs the three mandatory DD attribution files and both existing attribution
globs against the actual artifact in a separate pytest process. The two d1 lane
transport fixtures omitted the authorized caller they require; their exact reviewed
main versions add that fixture without changing assertions. The driver copies those
two reviewed files, the remaining candidate tests and candidate conftest into a
temporary harness, leaving the artifact unchanged. Candidate P1 boundary negative
tests remain included. Explicit candidate import paths and autouse origin assertions
bind attribution, route-to-lane and caller-boundary runtime modules to the artifact.
Both reviewed fixture hashes are pinned with the helper sources. It then runs ordinary
and profile-helper fixtures from the pinned tools source, passing the exact artifact
path to the real module-origin, fresh-home canonical default and DGX dispatch test.
Thus helper tests do not replace actual artifact coverage. Both phases isolate home
and provider environment before collection, refuse missing mandatory files, retain
failure exit codes, disable telemetry and install no dependencies.

The ordinary full release runner also includes the new profile fixture file, so
the helper's own merged source receives the normal main release proof. Artifact
proof and helper-main proof are distinct, genuine results; neither is hand-authored.

The prepared artifact passed the actual component driver: 32 attribution/P1 boundary
tests and 101 ordinary/profile/component tests, exit 0. Its first run exposed five
failures in the stale d1 authorized-caller setup; the reviewed fixture correction
above resolved those failures without changing runtime authorization or assertions.
The combined author-checkout helper/release-runner suite passed 106 tests. These are
offline test results, not a production activation or consumer canary.

## Per-profile stage and admission

The fixed allowlist contains ten named profiles under `~/.hermes/profiles`:
architect-standards, dd-design, dd-engineer-1/2/3, dd-pmo, devops-release,
knowledge-context, product-os and qa-review. Five separate-home profiles are azul,
finance, hardware, hyperscience and ptg. The named profiles retain their distinct
`--profile` argument; separate-home profiles retain `--profile default`. Ordinary,
Classic and arbitrary paths are not accepted targets.

Stage is read-only. For example, from the pinned tools clone:

```sh
python scripts/dd_hermes_profile_release.py stage --profile qa-review \
  --candidate /Users/openclaw/apps/hermes-profile-runtime-dgx-20260908 \
  --python /Users/openclaw/.hermes/hermes-agent/venv/bin/python \
  --target ed06e69a39ebbc8c31d9f9243651a915416c1778 \
  --reviewed-commit FULL_MERGED_HELPER_SHA
```

Save the manifest under a short private path, for example
`/Users/openclaw/.openclaw/hpr/m-qa-review.json`. `engine_for(profile).restart_command`
generates the exact manifest/hash-bound activation command. Use its unchanged text
in the queue registration and confirm every command is at most 500 characters.
The command includes one literal `gui/502/ai.hermes.gateway-<profile>` identity.

Register a separate `hermes-profile-<profile>` deploy service for each profile,
with `type:python`, `build:null`, `promote_source:false`, and the immutable artifact
as both `path` and `repo`. No invented listener port is necessary; MC supports
launchd identity for portless services. Retain profile-specific proof/operation
identity; a chained restart's first-label proof cannot represent fifteen processes.

Use one canary row first. After its approved activation and attended acceptance,
stage and submit the remaining profiles against then-current baselines and execute
them sequentially. Do not assume `depends_on` is accepted without readback. Every
approval belongs to Levi. If another approved ordinary release changes ordinary's
PID/source/config before the profile activation, restage and rebind the manifest;
do not waive the stale protection. Do not guess a future ordinary PID.

## Transaction and limits

Each engine instance has its own fixed label, home, plist and per-profile journal;
it does not rewrite the ordinary module's globals. A fleet lock serializes different
profile operations. The tested transaction persists operation consumption before
mutation and keeps exact private source/config/plist/interpreter identities and
backups. Approval must match service, artifact target, manifest command and queue
operation. Duplicate operations and unresolved failures remain fenced.

Only the target plist's cwd and `--replace` removal change; interpreter, environment,
home, profile arguments and all other properties stay intact. Only its existing
config's STT provider scalar changes to DGX. The old shared source and shared venv
are not edited. Protected ordinary/Classic snapshots bind PID, actual cwd,
plist/environment, config, tracked source and interpreter; they are checked before
each stop/start and at final validation.

Portless verification binds status-file PID to launchd/lsof identity and requires
idle baseline state. Enabled transports come from the guarded canonical resolver;
disabled historical entries are not requirements. A new process must report its
matching PID, candidate cwd, running state, fresh Telegram/other required transport
timestamps after start, and a short stability window. These are status-file checks,
not fabricated HTTP probes. There is no automatic message send.

The inherited transaction allows about 15 seconds for activation and 10 more for
one exact rollback; it never repeatedly kills processes or bootstraps over a
surviving old PID. Foreign edits are not overwritten. A turn can arrive after the
idle check, and normal shutdown can interrupt it. Helper/host death after mutation
has no external-supervisor recovery guarantee: consumed IDs and unresolved journal
fence replay, and an operator must inspect/restore the exact target profile. This
ordinary-profile limitation does not satisfy or relax Classic's separate contract.

Generic MC runtime proof is followed by an attended consumer acceptance check. A
connected status entry or fixture does not prove a real Telegram message exchange,
DGX availability at deployment time, or unattended recovery after process death.
