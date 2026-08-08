# 0019 — Session build protocol and the builder container contract

- Status: accepted
- Date: 2026-08-08

## Context

Part of the finalized remote-build architecture (see ADR 0017 for the
set; the input artifact is ADR 0018's build context). The shipped
protocol between dashboard and build server (dashboard ADR 0006) is
job-based: one shot, one build, artifacts, done. Three needs outgrow
it: **correctness** — state produced by earlier steps (prepared
workspace, applied patches, unpacked SDK) must survive into later
steps, and with digest-pinned containers the *version* race is already
impossible while the *state* race is not; **the dev loop** — warm
incremental rebuilds; **extensibility** — multi-phase flows
(build → test → package) without protocol redesign.

At the same time the builder container (ADR 0007) is to become a
replaceable part: any container satisfying a published contract is a
usable builder — including third-party ones with their own toolchains,
even NCS — transparently under the same infrastructure.

## Decision

### 1. The unit of interaction is a session

One session = one container instance = one effective context. The
session is the trust boundary. The same verb set has two backends:
**local** (the lib drives the container runtime directly) and
**remote** (the build server proxies the identical verbs, adding auth,
policy, scheduling). Transport for remote: WebSocket + bearer token,
as in the existing protocol (dashboard ADR 0006 decisions 1–2 carry
forward unchanged).

The price of sessions is stated with the decision: a session holds
resources while the client thinks — and a *busy* session is not a free
session. Shared servers therefore enforce admission by profile/cost
class, lease/heartbeat, idle timeout, hard TTL, per-user
concurrent-session quota, **and metering of actual work**
(CPU-seconds / build invocations against per-session and rolling
per-user budgets).

### 2. The verb set

- **`capabilities`** — pre-session, cheap, unmetered: protocol
  version, available builder images (tag + digest + contract labels),
  per-layer patch policy, quota/cost summary. The lib consults it
  during constraint resolution and fails fast ("server has no builder
  for zephyr-4.4.0-r1") instead of dying mid-session.
- **`open-session(manifest header, protocol + context format version,
  profile)`** — policy check + admission. The **response carries the
  negotiation**: server protocol version, supported manifest-format
  range, the serving container's contract version and command set,
  assigned cost class, session ID + lease. Discovery lives here — not
  in `verify`. Version mismatch is a typed rejection at the door,
  never a downstream failure. Container materialization is **lazy** —
  the backend may defer creating the container until the first command
  that needs one.
- **`send-context(archive)`** — upload the base context.
- **`extend-context(files)`** — per-layer **replace semantics** (add /
  overwrite / remove), repeatable. `manifest.yaml` itself is
  **immutable for the session's lifetime** — the session is bound to
  the header it was admitted on; a changed manifest is a new session.
  After every extension the server re-derives the patch-layer set from
  the files *actually present* and re-runs policy **and cost class** —
  patch semantics live entirely in the paths (ADR 0018 decision 2);
  there is no declared patch list that could disagree.
- **`verify`** — deep in-session assertion that the materialized
  environment matches the manifest pins. Optional; skipped on the fast
  path (discovery already happened at `open-session`).
- **`build [mode]`** — `clean` (fresh workspace; required for
  release/OTA artifacts — reproducibility and attribution; the first
  build in a freshly materialized container counts as clean) or
  `incremental` (warm workspace — the dev iteration loop, and the
  future script DEV mode maps onto it directly; results are
  session-private). Every command invocation gets a server-assigned
  **invocation ID**; outputs land in `/out/<invocation-id>/`. The
  result names the effective context ID actually built, so artifacts
  stay attributable.
- **`get-artifact(invocation ID, path)`** — streamed, verified against
  the hash from the result payload. Artifacts are fetchable throughout
  the session and for a bounded grace period after close.
- **`attach-session(session ID)`** — **connection loss is not
  abandonment**: a running build continues detached (heartbeat grace
  period generous relative to build duration, bounded by the TTL);
  events are buffered server-side with sequence numbers so a
  reconnecting client resumes streams from an offset. The idle timeout
  counts absent *commands*, not absent connections.
- **`close-session`** — reap the container. Leases + timeouts
  guarantee reaping when a client truly vanishes.

**Session profiles** (`oneshot` | `dev` | `test`) drive admission,
TTL, idle timeout and the per-profile resource budget; commands
outside the declared profile are rejected typed.

**Fast path:** `open-session → send-context → build → close-session`;
the lib offers a one-shot `build` convenience that does exactly this.

### 3. Events, errors, results

Events: while queued, position/ETA events; per command, a typed
progress stream plus a separate raw log stream; session-level events
(lease warning, eviction).

Errors: fixed envelope `{code, layer, retryable, message, details}`.
Codes come from an **append-only** dotted registry (`policy.*`,
`session.*`, `context.*`, `version.*`, `builder.*`; `x-*` reserved for
third parties); `retryable` is authoritative (clients never infer it);
unknown codes are treated as non-retryable-fatal with the message
surfaced.

Result payload (per `build`): artifact list with hashes — unsigned
`firmware.hex`/`firmware.bin`, unsigned OTA payload,
`build-report.json` (sizes, warnings, ccache stats, container digest
and effective context ID actually used). Signing happens client-side
(dashboard/CLI, detached imgtool, ADR 0015 §8) — unchanged.

### 4. The builder container contract v1 — frozen ABI

Any container satisfying the contract is a usable builder. The
normative specification is
[builder-container-contract.md](../design/builder-container-contract.md);
it doubles as the future public "bring your own builder" spec, which
is why its ABI is frozen deliberately. The load-bearing points:

- **Mounts:** `/ctx` (context, RO), `/sdk` (SDK package unpacked by
  the backend, hash-verified, RO), `/out` (RW), `/ccache` (optional
  volume; absent ⇒ cache lives in the container layer and dies with
  it).
- **Invocation ABI (frozen in contract v1):** the container starts
  idle and stays alive for the session; each command is executed as
  `mcuhome-builder <command> /ctx --out /out` — this argv **never
  grows**. All command parameters arrive as one JSON document at a
  fixed path (`/ctx/.mcuhome/command.json`, written by the backend per
  exec, includes the invocation ID). Reserved exit codes: 64 =
  unsupported command, 65 = unsupported *required* parameter;
  non-required unknown parameters must be ignored. Machine result at
  `/out/<invocation-id>/result.json`, versioned (`result: 1`) with an
  enumerated `status` set. Third-party containers only need to ship
  the `mcuhome-builder` multiplexer (script or binary).
- **Progress:** the builder emits NDJSON progress events
  (`{phase, current, total, message}`); the backend relays them as
  typed events. Raw logs are a separate stream.
- **Network: none during the build** (backend-enforced). Everything a
  build needs is mounted.
- **Labels:** `org.mcuhome.contract=1`, `org.mcuhome.zephyr=<ver>`,
  `org.mcuhome.toolchain=<id>`,
  `org.mcuhome.commands=verify,build[,…]` — the server-facing source
  for capability/compatibility checks (the client-facing source is the
  `open-session` response). The commands label is how third-party
  containers advertise extra capabilities (e.g. `test`).

### 5. Patched layers: backend-provided copy-on-write views

The RO mounts and image trees are deliberate — they are the
always-pristine baseline. For a layer that carries patches, the
**backend** hands the container a *writable view* of that layer built
as an overlay (lowerdir = pristine RO source, upperdir = per-session
scratch on the host; with Docker via local-volume driver options). The
builder applies patches to the view and builds against it.

Placement is a **security decision**: the overlay is constructed
**host-side by the backend, never mounted from inside the
container** — in-container mounts would require mount privileges
(CAP_SYS_ADMIN) in the exact container that executes untrusted patch
code. Copying the layer into the workspace remains the fallback
mechanism for small layers or backends without overlay support; the
contract only guarantees the *behavior* (writable view, restorable
pristine baseline), never the mechanism. The same primitive is
reusable later for other RO bases (e.g. an overlay over the RO
ccache — parked; the ccache-native RO-secondary setup stays the
simpler default).

**Layer reset is a backend responsibility:** the backend knows when a
layer's patch set changed (it processed `extend-context`) and resets
that layer's view before the next command — overlay: discard the
upperdir (exact and cheap; a pristine reset is *not* possible from
inside the merged view — deleting a file there creates a whiteout
instead of restoring the base); copy fallback: re-copy. The builder
records the applied patch-set hash per layer and reapplies patches
after a reset. Incrementality survives only for untouched layers.
(This prevents v1/v2 patch mixtures being attributed to the v2 context
hash.)

### 6. ccache

Public servers mount the volume RO and configure it as ccache
**secondary storage** (container-local primary stays RW, discarded
with the session) — jobs benefit from the warm cache and cache within
their own build without ever writing the shared store. Own servers
mount RW. Warming = deliberate operator invocation with RW mount,
**trusted contexts only (no user patches)**.

### 7. Server policy: the patches config IS the policy

No dev/strict modes. The server's builder config is the policy;
unlisted patch layers are **denied by default**. Presets ship only as
documented example configs.

```yaml
builders:
  default:
    scheduling: dynamic        # resource-weighted; cost estimated from the
                               # effective context (patch layers ⇒ cost class)
    patches:
      sdk:    { allow: true }  # cheap: busts only SDK-layer cache
      zephyr: { allow: false } # expensive: busts shared warm cache
      chip:   { allow: false }
    ccache: { volume: /srv/ccache, mode: ro }
```

### 8. Hardening floor for shared servers

All violations are typed errors.

- **Ingress caps**, enforced *streaming* during upload/extraction —
  never trusting declared sizes: max compressed size, max cumulative
  decompressed size, max entry count, max per-file size, max path
  depth. (Guards against decompression bombs filling the host before
  any build starts.)
- **Safe extraction:** only regular files and directories; reject
  absolute paths, `..` after normalization, symlinks/hardlinks/
  devices. Writes confined to whitelisted subtrees (`model/`,
  `patches/<layer>/`), into a per-session directory the server owns.
  (Zip-slip guard; also part of the container contract, so third-party
  tooling cannot reintroduce the hole.)
- **Never trust client-declared hashes:** the server recomputes every
  file hash and the context ID from the received bytes and rejects on
  mismatch — declared values are advisory. (Integrity and attribution
  requirement — independent of any caching.)
- **SDK/container acquisition:** `package.url` is a hint only. The
  backend resolves (name, version, sha256) against its **configured
  source list** into a content-addressed, immutable, fetch-once store;
  container images are pulled only from configured registries. (SSRF
  and disk-fill guard; also makes SDK fetch per-session cheap.)
  Sources are fully operator-configurable — official registry
  (default), own mirrors, plain directories for offline or unreleased
  packages:

  ```yaml
  sdk_sources:
    - https://packages.mcuhome.org        # default (official)
    - https://mirror.example.com/mcuhome  # own mirror
    - /srv/mcuhome/packages               # local dir (dev/offline)
  ```

  The decisive point is only *who* configures the list: the backend
  operator, never the client manifest. Locally the user IS the
  operator, so local builds can use any source they like.
- **Per-session disk quota** on workspace and `/out` (typed
  quota-exceeded instead of host exhaustion), plus the work metering
  from decision 1.

Isolation rules: one **session** = one ephemeral container instance
(the session is the trust boundary), no network during build, resource
limits per session, shared ccache never writable by jobs. Session
control: lease/heartbeat, idle timeout, hard TTL, per-user
concurrent-session quota.

## Consequences

- Dashboard ADR 0006's transport and threat-model decisions carry
  forward; its job-frame vocabulary and `GET /capabilities` endpoint
  are replaced by the session verbs above. The repository consequence
  (the build server leaving the dashboard repo) is dashboard ADR 0012.
- The container contract is a public commitment in the making: its
  ABI, exit codes, result format and label set can only be extended,
  never changed — extensions ride on new commands (advertised via the
  commands label) and on format-version bumps, exactly like the
  context hash rule of ADR 0018.
- Both error-code registry and verb set are append-only; clients treat
  unknown codes as fatal and never infer retryability. Protocol
  evolution has one place to happen: the `open-session` negotiation.
- A local build and a remote build are the same protocol, so every
  feature (patches, incremental builds, profiles) exists exactly once
  and works identically in both.
- Shared servers get a defined security floor rather than advice:
  policy is configuration, violations are typed, and nothing the
  client declares is trusted.
- Sessions hold resources; the admission/lease/metering machinery of
  decision 1 is not optional hardening but the cost of the model,
  recorded with it.
- Related standing decisions: ADR 0007 (the container this contract
  generalizes), ADR 0013 (build profiles), ADR 0015 §8 (client-side
  signing), ADR 0017, ADR 0018; dashboard ADR 0006 (transport),
  ADR 0007 (wire content), ADR 0012.
