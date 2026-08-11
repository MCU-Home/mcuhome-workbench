# 0019 — Session build protocol and the build-container contract

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
[build-container-contract.md](../design/build-container-contract.md)
(renamed with the term, 2026-08-09 — see the amendment below); it
doubles as the future public "bring your own build container" spec,
which is why its ABI is frozen deliberately. The load-bearing points as
first recorded — **five of them are superseded by the amendment below
and by the contract, and are kept here as history: the mount table, the
invocation ABI, the exit codes, the progress channel, and the
`org.mcuhome.commands` label** (contract §4 defines no mount points,
§5 the invocation and the exit codes, §8 the event channel, and §2.1 no
longer carries a commands label at all — `program.actions` is the one
declaration of the action set):

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

> **Superseded 2026-08-09 — there is no layer reset.** The paragraph
> below, and the words "restorable pristine baseline" in the guarantee
> above, are removed by the amendment at the end of this document: with
> `lock-context` the patch set of a locked context cannot change, so the
> condition this apparatus reacts to cannot occur. What survives is the
> writable view, the host-side placement, and the whiteout observation
> the last sentences make.

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
  **`keys/`** (added 2026-08-09 with the signing key becoming context
  content — ADR 0018's amendment of the same date), `patches/<layer>/`)
  plus the context's entry file `context.yaml` at the root, into a
  per-session directory the server owns. `manifest.yaml` is written by
  the backend at `lock-context` and is never an extraction target.
  (Zip-slip guard; also part of the container contract, so third-party
  tooling cannot reintroduce the hole.)
- **Never trust client-declared hashes:** the server recomputes every
  file hash and the context ID from the received bytes and rejects on
  mismatch — declared values are advisory. (Integrity and attribution
  requirement — independent of any caching.) **Recomputing file hashes
  is not the whole check** (added 2026-08-09): three of the four hashed
  inputs — `container.digest`, `sdk.sha256` and `target.board` — are
  declared values that no file hash can measure, and the context header
  that declares them is itself outside the integrity list by
  construction. The backend MUST therefore also verify the container
  digest against the image it actually pulled, the SDK hash against the
  package bytes it actually fetched and unpacked, and the board against
  the pins the session was admitted on. Without it a self-consistently
  forged header verifies clean; the defect that showed this is recorded
  in ADR 0018's amendment of the same date.
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
    - /srv/mcuhome/packages               # local dir (dev/offline/unreleased)
    - https://packages.mcuhome.org        # the official index
    - https://mirror.example.com/mcuhome  # any other external source
  ```

  (Order corrected 2026-08-09 — the example carried the reverse of the
  decided search order; see the amendment below.)

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
  never changed — extensions ride on new actions (advertised in
  `program.actions`) and on format-version bumps, exactly like the
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

## Amendment: the 2026-08-08 layer, backend profiles, the freeze verb, cancellation, and the invocation ABI (2026-08-09, product owner)

Terminology first, because it runs through everything below: the build
environment is the **build container** and the orchestrator is the
**build server**; "builder" is retired as a term, and the normative
document is [`build-container-contract.md`](../design/build-container-contract.md).
Where this ADR's original text says "builder", read "build container";
where it says "the lib", read the package set of ADR 0020. The ADR's own
title was corrected with the term; the filename was deliberately not,
because an ADR's number is its identity and a rename would break every
cross-reference already pointing at it.

That read-through is about **prose, not identifiers**. Decision 3's
error-code registry carries a `builder.*` prefix, and a prefix is a wire
value that clients match on rather than a word a reader interprets, so
it is not renamed by a terminology note. Nothing is implemented against
it and the registry is append-only from its first published entry, so
its spelling is settled when the registry is — not here, by side effect.

**The 2026-08-08 layer is the only valid remote-build concept.** The
remote-build architecture was planned once, found wanting, and
re-decided on 2026-08-08. What is valid is that layer and nothing
older: ADR 0017, ADR 0018, this ADR, ADR 0020, the build-container
contract, dashboard ADR 0012 — together with this amendment.

Everything older that concerns the build server is obsolete for this
subject and is **dismantled, not migrated**: dashboard ADR 0003 (two
Home Assistant Apps, with "the build server *is* the toolchain
container"), dashboard ADR 0006 (the job protocol), dashboard ADR 0011
(builder coupling / "Block 0"), and `docs/design/builder-pipeline.md`
§5/§6. Dismantled means the text goes away or is marked superseded
where it stands; it does not mean its concepts are carried into the new
protocol and renamed.

That has to be read together with this ADR's own Consequences, which
say dashboard ADR 0006's transport and threat-model decisions carry
forward. They do — but the carry-forward is stated by dashboard
ADR 0012, not by ADR 0006 surviving as a standing decision. The
explicit list is: WebSocket plus bearer token, TLS at the deployment,
the leaked-token threat model, the mDNS amendment; "the dashboard never
compiles"; and user key handling with detached signing on the dashboard
side.

**`lock-context`: the context is frozen by an explicit verb.** The
session and the build context have different lifetimes, and the verb
set of decision 2 gains one verb — append-only, as the Consequences
require — to say where the boundary is:

```
open-session       session id, lease, version negotiation (no context yet)
send-context       base context incl. the pins; the container can be created
extend-context     repeatable; MUST NOT touch the pin file
[read-only commands permitted]
lock-context       freezes the context, writes manifest.yaml, computes and
                   returns the context id; unlocks the writing commands
verify / build     only from here
get-artifact
close-session
```

The alternative was an implicit freeze on "the first writing command".
It was rejected for a structural reason: it needs an enumerated list of
writing commands, and that list has to be kept in sync with a verb set
that is append-only by decision — a third-party command could not know
which side of the line it falls on. The explicit verb buys three more
things. The context ID gets an **observable moment**, at which both
sides compare values they computed independently (contract §3.3). It
makes `verify` meaningful, because there is a stable `files` list to
check against. And it yields clean typed errors — `context.locked`,
`context.not-locked` — instead of a command that quietly means
something different depending on what ran before it.

Consequences inside decision 2: `open-session(manifest header, …)`
loses its first operand — admission negotiates protocol and
context-format version and profile, and the pins arrive with
`send-context` (ADR 0018's amendment retires "manifest header" and
names the pin file `context.yaml`). `extend-context`'s "`manifest.yaml`
is immutable for the session's lifetime" is replaced: before the lock
there is no manifest, `context.yaml` is what may not change, and after
the lock the context is closed to writes entirely.

**Container-specific discovery moves to the `send-context` response.**
Decision 2 puts the whole negotiation in the `open-session` response,
including "the serving container's contract version and command set".
Half of that is no longer answerable there. With no context at
`open-session` the backend does not yet know **which** build container
serves the session: the container digest arrives with the pins, in
`send-context`, and lazy materialization means the container itself may
not exist before that either.

The response is therefore split along the line the flow already draws.
`open-session` keeps what admission alone decides — the server's
protocol version, the supported context-format range, the session ID,
the lease and the backend profile. `send-context` answers what the
context determines — the serving build container's contract version and
its command set. This adds nothing to the protocol and answers nothing
twice; it is decision 2's discovery payload, delivered at the first
moment each half of it is knowable.

What decision 2 wanted from discovering early survives intact: a version
or capability mismatch is still a typed rejection before any work is
scheduled, because `send-context` precedes `lock-context` and therefore
precedes every working command. And ahead of a session at all,
`capabilities` remains the pre-session query that lets the workbench
choose a container during pin resolution rather than discover the
mismatch from inside one.

**`cancel(invocation-id)`.** Aborts the running invocation; the session
and its warm container survive. It is added now, while the verb set is
still free to grow cheaply, and it is a necessity rather than a
convenience in the container profile for one concrete reason: **killing
a `docker exec` client does not stop the process inside the
container.** A local backend that merely drops the exec connection
leaves the compile running and the session's resources held, so it
needs an explicit cancel path rather than a closed socket. This is the
deliberate counterpart to `attach-session` in decision 2 — connection
loss is never abandonment, so cancellation must be something a client
*says*.

**The build server is an orchestrator, and it has two deployment
targets.** It drives build environments and is never one itself.
**Standalone and self-hosted is the primary target**: a machine an
operator installs the service on and reaches over the transport of
decision 1. The **Home Assistant App is an additional target** — not the
shape the design is drawn around — and it is served by the subprocess
profile below rather than by a second architecture.

Naming which target is primary is load-bearing for the rest of this
amendment. It is why deny-by-default patch policy, the ingress caps and
the whole hardening floor of decision 8 are written for a server that
strangers may reach; and it is why the reduced guarantees of the
subprocess profile are acceptable at all, since they belong to the
deployment in which operator, user and device owner are the same person.

**SDK package sourcing, and the order the search runs in.** Decision 8
leaves the source list to the operator; this amendment fixes the order
it is searched in: **a local directory first, then
`packages.mcuhome.org`, then any other external source.** Decision 8's
example config carried the reverse and is corrected above.

Local first is what makes the ordinary cases work without argument. CI
builds and hashes the `mcuhome-sdk-<version>` archive (ADR 0018
decision 6), and a directory holding that archive is the whole first
implementation — enough to run every build method of ADR 0020 decision 6
before any index is published. A local directory is also the only way an
unreleased or offline package is usable at all, so a search order that
reaches the network first would make the development case the awkward
one. `packages.mcuhome.org` follows as the official source, and the
static index behind it is built as part of this work. Anything else — a
mirror, a vendor's own server — comes last, because a source this
project does not operate should never shadow one it does.

Users providing their own SDK packages is an **explicit goal**, not a
tolerated side effect. The same list that lets an operator point at a
mirror lets a user point at a package they built themselves, and the
"bring your own build container" premise of decision 4 would be worth
little if the SDK inside that container could only come from one host.

Reordering the list changes nothing about identity: `package.url` stays
a hint that is never hashed (ADR 0018 §6), so the same context fetched
from a local directory and from `packages.mcuhome.org` has the same
context ID. The sha256 is what must match; where the bytes came from is
not part of what a build reproduces.

**Two backend profiles, and what each one guarantees.** Decision 1
names two backends by transport (local and remote). This amendment
names two by build environment, which is the axis the isolation rules
actually hang on:

- **container** — the backend materializes one container per session.
  Every isolation guarantee of decision 8 applies unchanged.
- **subprocess** — the build environment runs **in the same filesystem
  as the build server**, but as a **separate process** (the Home
  Assistant App case). The backend runs the build program as a
  subprocess in its own filesystem namespace instead of via `docker
  exec`. Same invocation ABI, same request document, same result
  document; `/ctx`, `/sdk` and `/out` are ordinary directories rather
  than mount points, which is precisely why the ABI below no longer
  freezes paths.

  What is shared here is the **filesystem, not the process**, and that
  distinction is what lets "the build server drives build environments
  and is never one itself" hold without an exception — the sentence
  this amendment opens the deployment-targets paragraph with, and the
  one this profile would otherwise contradict. Even
  here the build server orchestrates: it materializes the paths, invokes
  the program and reads its result. Contract §1.2 states it in the same
  words.

Subprocess, not literally in-process, and the build server's own code
already argues the point for itself
(`build-server/mcuhome_buildserver/builder.py:5-28`): a build running
inside the server process cannot be killed without killing the server;
an out-of-memory kill or a segfaulting compiler takes the queue, the
job history and every connected client with it; and only a separate
process is honest about the interface. Loading the program into the
server would additionally make MCUHome's own Python implementation the
only implementable one, against this ADR's premise that a third party
may ship its own, and would put third-party patch code in the address
space of the Home Assistant App.

The `open-session` response declares which profile serves the session.
A subprocess backend serves exactly one build environment — the one it
is — and rejects a session whose `container.digest` it does not match,
typed.

Its reduced guarantees are named rather than implied: **no network
isolation, no per-session resource limits, no container trust
boundary.** Decision 1's "the session is the trust boundary" is a
statement about the container profile.

**Limits are per server; the per-user machinery belongs to the hosted
phase.** v1.0 is single-tenant. Maximum concurrent sessions, TTL, idle
timeout, disk budget and the compile-lane limit are all **per server**,
and there is no work metering and no cost classes.

The reason is that the per-user machinery has no subject today: the
transport carried forward is WebSocket plus a bearer token, and **one
bearer token is one principal**. A deployment holds one token, so a
per-user quota would be a per-server quota with a misleading name, and
metering would bill one principal for its own machine. Decision 1's
per-user concurrent-session quota and its "metering of actual work",
decision 2's assigned cost class, decision 7's cost-class scheduling
comment and decision 8's repetition of the quota are therefore bound to
the hosted phase (dashboard ADR 0006's post-1.0 outlook) rather than
implemented now.

Decision 8's hardening floor is untouched by this, and that is the
point of separating the two: it is identity-independent. Ingress caps,
safe extraction, never trusting declared hashes, an operator-controlled
source list and the per-session disk quota all mean exactly the same
thing with one principal as with a thousand.

**Context and artifacts are destroyed at `close-session`.** The
per-session directory — the context and every artifact in it — is
deleted when the session closes. Artifact download therefore happens
inside the session, after the build and before closing.

Decision 2's `get-artifact` sentence "artifacts are fetchable
throughout the session and for a bounded grace period after close" is
**removed**, and with it an undefined bound: nothing said how long the
grace period was, while the directory it kept alive holds a device's
Matter commissioning credentials (dashboard ADR 0007 decision 2) and is
archivable by design (ADR 0018 decision 5). An unbounded retention of
exactly that material is the wrong default to leave standing in a
protocol whose evolution is append-only.

Recorded direction, confirmed by the product owner and explicitly
**not** a v1.0 requirement: commissioning credentials are to move out
of the build entirely — injected into the image locally after the
build, exactly as the signature already is (ADR 0015 §8) — so that
neither build server nor build container ever receives them. That is
dashboard ADR 0007 decision 5. The consequence for design work now is
narrow and binding: nothing may entrench credential handling on the
build side.

**Patches in the subprocess profile: denied by default, enablable by
the operator.** Decision 7 is unchanged — the patches config *is* the
policy and unlisted layers are denied. What this settles is that the
subprocess profile is not categorically excluded from patching: an
operator may enable layers there.

The recorded reasoning is about who the two deployments actually serve.
A standalone build server may be publicly reachable and used by
strangers; the Home Assistant App variant is normally used by one
person, for their own devices, running their own patches. Deny-by-
default keeps the first case safe, and the operator switch keeps the
second usable. That it is the operator's explicit act matters more here
than in the container profile, because this is the profile with no
container trust boundary to fall back on.

**Layer reset is removed; a broken patch application ends the
session.** Decision 5's layer-reset apparatus disappears from the
frozen surface, and its "restorable pristine baseline" guarantee goes
with it. Its only stated purpose is a case `lock-context` has made
unreachable: the backend was to reset a layer's view **when that
layer's patch set changed between invocations**, so that a v1/v2 patch
mixture could not be attributed to the v2 context hash. Patches can
only arrive before the lock, and no working command runs before the
lock, so the patch set is constant for the whole life of a locked
context and the triggering condition cannot occur.

Three obligations go away with it: the backend's duty to reset a
layer's view; the build container's duty to record the applied
patch-set identity per layer, compare it on every invocation and
reapply on mismatch — it collapses to **apply once per session**; and
the per-tree `generation` counter that was proposed for detecting a
reset (contract §6.2).

**Interrupted patch application** — a crash, a cancellation or an
out-of-memory kill after some patches but before all — fails typed, and
every further working command in that session is refused. The client's
remedy is a **new session**, and therefore a new container with
pristine trees. The reasoning is that a retry buys nothing: the patch
set is frozen, so a broken patch fails identically, and after a crash a
new session costs a container start and a cold build; a recovery
mechanism would be two frozen contract obligations for a case a new
session already resolves. Decision 5's own observation is what makes
the failure terminal rather than recoverable in place — a pristine
reset is not possible from inside the merged view, because deleting a
file there creates a whiteout instead of restoring the base.

**The invocation ABI.** Decision 4's invocation bullet and its exit
codes are superseded; the normative text is the build-container
contract, and what belongs here is what changed and why.

- **The invocation.** `mcuhome-builder <command> /ctx --out /out`
  becomes `/mcuhome/run <action> <absolute path of the request
  document>` — two positional operands, never a flag. A fixed absolute
  path rather than a bare name on `$PATH`: the image author controls
  `PATH`, `docker exec` inherits the environment fixed at container
  creation, and the name resolves without a shell, so a bare name is a
  promise about a filesystem MCUHome does not control. `/mcuhome/`
  reserves a namespace in the image and carries the brand; the filename
  names the role rather than the action, because the action is an
  argument and cannot also be the name; and there is no extension,
  because a third party may ship a compiled binary and `.sh` would then
  be a lie. Precedent for both halves: Cloud Native Buildpacks
  (`/cnb/lifecycle/*`, `bin/detect`, `bin/build`) and Dev Container
  Features (`install.sh`) each fix an absolute path, and neither brands
  the executable with the vendor's name.
- **Everything else travels in the request document**, which lives in a
  backend-owned per-invocation directory — never inside the context.
  That makes the context a genuinely read-only mount, and it removes a
  race the fixed path `/ctx/.mcuhome/command.json` has today, where two
  concurrent execs overwrite each other's document.
- **The program assembles its own build environment.** West workspace,
  module registration, `ZEPHYR_BASE`, `CHIP_ROOT` — the program builds
  them from the trees it is given, and the backend never supplies a
  workspace. A set of paths is not a build: the generated CMakeLists
  resolves `${ZEPHYR_MCUHOME_MODULE_DIR}`
  (`mcuhome/compiler/generate.py:1343`, `:1354`) and searches for `CHIP_ROOT`
  via `$ENV{ZEPHYR_BASE}/../modules/lib/connectedhomeip`
  (`mcuhome/compiler/generate.py:1200-1215`), and neither exists outside a
  registered Zephyr module tree. Assigning the responsibility is what
  makes the contract implementable by an image with a different
  topology — an NCS one, say; freezing topology fields would have
  frozen MCUHome's own layout instead.
- **Exit codes 0 / 1 / 66.** `0` — the invocation ran and the work
  succeeded; a result document exists. `1` — the invocation ran and the
  work did not succeed; a result document exists. `66` — the request
  was unusable and no result could be addressed; no result document.
  Anything else means the program died, and stays undefined forever.
  Decision 4's reserved `64` and `65` are dropped: they are `EX_USAGE`
  and `EX_DATAERR` from BSD `sysexits.h`, which foreign runtimes emit
  for ordinary argument errors, so a Go program returning 64 on a typo
  would be read as "command not supported" and have its work
  rescheduled. Why a command was refused is an enumerable and growing
  list, which by this ADR's own evolution rule makes it document
  content rather than a frozen number. Nothing is deployed, so
  continuity costs nothing here.
- **Why the shape is this small, restated because it is the frozen
  part.** The entry point exists so a user can build and run a
  completely own build container and still work inside MCUHome's
  system. Extensibility therefore rides on the JSON document and never
  on new argv parameters: a new parameter breaks every third-party
  container that does not know it, while a new JSON field is simply
  ignored by an older one. This must never change again.

**The frozen surface was cut before signing.** Six items were removed
from the contract because they carried nothing, and nothing in this ADR
depends on any of them:

- **`error.code` and `error.layer` beside `reason`** — one value in
  three frozen places. Only `reason` is in the result document; a
  backend embedding it into decision 3's envelope derives that
  envelope's fields from `reason`, and how it does so is the backend's
  business rather than contract text.
- **`artifacts[].size`** — decision 8 never trusts a declared size on
  ingress and the contract's egress hardening caps sizes from the bytes
  it enumerates. The declared number had no reader on either side.
- **the request document's `invocation` field** — the program had no use
  for it beyond echoing it back. The **server-assigned invocation ID
  stays**: it is what decision 2's `get-artifact` and the `cancel` verb
  above address, and the backend never had to name it to the program,
  which addresses an invocation by the paths it was handed.
- **`org.mcuhome.commands`** — a non-authoritative duplicate of
  `program.actions`. The backend loses the ability to pre-filter images
  by action set, which costs nothing: every conforming program
  implements `describe`, `verify` and `build`, and any action beyond
  those three needs `describe` to be trusted anyway.
- **the separate `message` field beside `error.message`** — two
  untrusted-text fields with one handling rule.
- **`layers[].count`** — derivable from the patch files the backend
  already holds.

No capability is lost by any of the six, and each one removed is one
thing fewer for a third-party implementer to get right.

## Amendment: the wire shape of the two 2026-08-09 verbs, and the session's end (2026-08-09 evening, product owner)

The first amendment added `lock-context` and `cancel` to the verb set
and said what each does; it did not say what travels on the wire. The
build server's implementation stopped exactly there — a typed refusal
rather than a stub that would have decided the shape by existing — and
the product owner settled the questions the same evening. Three
decisions, and the reasoning each one was taken on.

**`lock-context` is minimal: the request carries `session_id` and
nothing else, the response carries the context ID and nothing else.**
The alternative — the client sending its own independently computed ID
for the server to compare — was considered and rejected in favour of
the smallest possible protocol surface. The comparison the first
amendment requires ("both sides compare values they computed
independently") therefore happens **on the client**: the workbench
computes the ID from the bytes it sent, compares it against the ID the
response carries, and closes the session on a disagreement. The
consequence is stated here so nobody rediscovers it as a gap: the
server never sees the client's value, so the server cannot raise a
mismatch — holding the workbench to its comparison duty is a
requirement on the session client (the `remote` build method), not on
the protocol. A third-party client that skips the comparison builds on
a context that is not the one it thinks it sent, and no server-side
check exists to catch it; the conformance obligation belongs in the
client's own suite.

**`cancel(session_id, invocation_id)` acknowledges immediately.** The
answer means "the stop signal is set", never "the invocation has
stopped". The actual end travels on the channel that already exists:
the invocation's event stream, and a result document whose `status` is
`cancelled` (contract §5.4). A verb that blocked until the invocation
stood would hang on the socket for up to `cancel_grace_seconds`,
inherit every reconnect question `attach-session` exists to answer,
and need rules for a second `cancel` racing the first — three costs
for a guarantee the result document already gives. Two edges are
typed: an `invocation_id` the session does not know is the error
`invocation.unknown`; an invocation that has already finished is
answered `already_finished` and is **not** an error, because the race
between a cancel and a natural completion is legitimate and both
parties behaved correctly.

**A poisoned session refuses work but keeps its artifacts.** The
interrupted patch application of the first amendment ("fails typed,
and every further working command in that session is refused") gets
its error code: `session.poisoned`, raised for every further working
command. The session is deliberately **not** reaped on the spot:
`get-artifact` and `close-session` stay permitted, because the moment
a session poisons is exactly the moment its owner most wants the logs
and partial artifacts that explain what happened, and destroying them
to simplify the state machine would trade diagnosis for tidiness.
Cleanup happens where it always happens — `close-session` or lease
expiry.

**`close-session` on a busy session cancels implicitly.** The running
invocation receives the cancel signal, its result document is still
written, then the session is reaped. Refusing to close while an
invocation runs was rejected because connection loss is never
abandonment (that is `attach-session`'s reason to exist), so closing
must never require a live client to first cancel, reattach, or wait —
a crashed client's session would otherwise hold its resources until
lease expiry as the *normal* path rather than the fallback.

Four smaller determinations, recorded with the same append-only
intent: every authenticated session verb counts as a command for the
idle timeout and refreshes it — one rule, no per-verb list, and
`lock-context` and `cancel` are commands like any other. The
"[read-only commands permitted]" line in the flow above names, today,
`capabilities`, `attach-session`, `close-session` and `cancel`; no
context-reading verb exists yet, and the line stays as the place one
would slot in. Both new verbs are permitted in all three session
profiles — `oneshot` needs the lock to ever reach `build`, and a
cancel must be possible wherever a build is. And a context that was
sent but is empty may be locked: it has a well-defined ID, and the
things a `build` needs beyond existence — `keys/signing.pub` above
all — are checked by `build`, which is where the contract scopes
them, not by the lock.

## Amendment: the writable view is the container layer (2026-08-10, product owner)

The supersession note above kept "the writable view, the host-side
placement" from the layer-reset paragraph. Half of that survived one
review too many: **in the container profile there is no host-side
placement**, and the product owner's observation that dissolved it is
recorded because it simplifies the backend to nothing.

The image's trees are already CI-patched at image build (the baked
workspace applies `patches/` — that is what the image *is* since r3),
and inside the container they are writable through the container's own
copy-on-write layer. One session is one container, and the container is
discarded at `close-session` — so a context-patched `zephyr` never
outlives the session that patched it, which is the entire isolation the
writable view exists to provide. The backend therefore asserts
`writable: true` for an in-image tree at the path `describe` reported,
truthfully, and constructs nothing: no overlay, no copy, no volume. The
program applies the context's patches in-container with the §6.2
machinery it already has.

Host-side construction remains real exactly where a container layer
does not exist: the `subprocess` profile — whose build environment is
persistent, which is why patches there are opt-in at the operator's own
risk (decision E11), and why contract §6.2 now states the two profiles
separately.

`local-dev` has no context-patch step at all, by design rather than by
gap: the developer's own west workspace is the build environment, and
its patches are applied by the developer's hands — a locally modified
Zephyr to chase a bug or work on MCUHome's core is the reason the mode
exists. The workbench's `local-dev` method builds what is there and
attributes it accordingly.
## Amendment: what the session client may rely on (2026-08-10 evening, product owner)

Writing the first session client against the protocol surfaced four
places where the implementation had decided a shape by existing, or
where a duty had an implementation but no owner. The product owner
settled all four; the reasoning travels with each.

**The `capabilities` answer announces the ingress caps** (`E57`). The
caps of the context transport — decompressed and compressed archive
size, entry count, per-file size, chunk count — exist so a client can
refuse an oversized upload before the first byte leaves, and a cap the
client cannot see can only be discovered by hitting it. The answer
gains an `ingress` object carrying them, plus the one transport bound
that lives below the verbs: the maximum WebSocket frame the server
accepts (today 8 MiB), whose overrun is a dropped connection rather
than a typed refusal and therefore must be knowable in advance. The
key names are the ones the first client already reads — its documented
forward-guess becomes the shape, one server change and none on the
client.

**The server's completion verdict is `invocation.verdict`** (`E58`).
E46 gave the verdict the same name as the contract's §8 program event
`invocation.finished`, distinguishable only by carrying no `seq` — so
a program that violated §8 by omitting `seq` would have its own
announcement read as the server's verdict. The contract is frozen and
keeps its event name; the session layer is not public yet, so the
verdict is renamed while renaming costs nothing. The discrimination is
now structural, not the absence of a field.

**Replay deduplication is the client's duty** (`E59`). After
`attach-session` the server replays literally from `from_seq` — it
keeps no per-client delivery ledger, exactly as `lock-context` keeps
no per-client comparison. A client that asks low legitimately sees
events again (that is what makes the replay a debugging tool), and the
client that wants exactly-once folds duplicates by the highest `seq`
it has seen per invocation. Recorded here as the E37 pattern applied
to events: minimal server, a stated client duty, conformance owned by
the client's suite.

**The `send-context` answer is part of the protocol** (`E60`). The
answer carries `pins` (what the pins file resolved to) and `container`
(the image the session will build in, contract version included), and
until now their field names were whatever the server's serializers
spelled — a client reading them was coupled to an accident. The shapes
are fixed as they are spelled today; what a client may read is named,
and a third-party server knows what it owes.

## Amendment: the context requires a Zephyr line, the server chooses the container (2026-08-11, product owner)

Scheibe 4 was written on the assumption that `context.yaml` pins a build
container by digest, and the product owner corrected it in one sentence:
*"den digest pinnt doch erst der server selbst?! die cli selbst pinnt nur
eine bestimmte zephyr version … der build server nutzt den passenden
container, und schreibt den exakten digest in das manifest. oder
antwortet mit einem fehler wenn er den versions pin nicht erfüllen
kann."* The assumption asked the wrong party: a client knows which Zephyr
its device needs and cannot know which images a given server holds, so a
client-chosen digest is either a guess or a round trip that has to happen
before a context can exist at all — a round trip this protocol would then
have to keep offering forever.

The context format is version 2 (E61). The requirement is the canonical
model's `toolchain.zephyr_line` (ADR 0013), carried informationally in
both context documents as `zephyr:` so a server can select without
parsing the model; the context ID hashes `sdk.sha256`, `target.board` and
the file list, and nothing else. The full rule and its reasoning are in
`docs/design/build-container-contract.md` §3.2, §3.3 and §11. Format 1
disappears without migration — nothing was published against it.

What this changes in the protocol, and only this:

**`send-context` answers the container the *server* chose.** The
E60 field names hold and their meaning moves: `pins` is still what the
client sent and therefore loses its `container` block and gains `zephyr`,
while the answer's own `container` object is now this server's
resolution. It carries `image`, `tag` and `digest` — the three names
`manifest.yaml` records, so what a client reads here and what it reads
off the manifest are the same fields — beside the `contract`, `program`,
`version` and `actions` that E60 already fixed. `digest` may be `null`,
for an image the server built locally and never pushed: such an image
names no fetchable bytes, and saying so is the honest reading of a field
whose names are now promises.

**An unsatisfiable line is `version.builder-unsatisfiable`**, at
`send-context`, before the context is frozen. It is a new registry entry
rather than a widening of `version.builder-unavailable`, because the two
answer different questions and a client can act on exactly one of them:
`builder-unavailable` is about *one* image and is actionable only by the
server's operator, while this one is about the server's whole inventory
and carries `required` (the line) and `available` (the lines actually
served) — enough for a client to choose another server or another
`zephyr_version` without anybody touching that host. Neither is
retryable; nothing is pulled.

**`lock-context` is unchanged in shape** and gains a duty. Its request is
still `session_id` and its response still the context ID and nothing else
(E37), and the client still owns the comparison. What the freeze now also
writes is the chosen container into `manifest.yaml` — outside the ID by
construction, which is what lets two servers serving one Zephyr line
freeze the same bytes to the same identity while each manifest records,
exactly, what built there.

**The pre-invocation re-check compares `zephyr` where it used to compare
`container.digest`.** The digest left the comparison because it stopped
being a value a client sent: it is the server's own record now, and
comparing it against the pins would be the server checking itself. The
line took its place and earns it — it is what the choice was made
against, and it is hashed nowhere, so no other check on that path would
notice it being rewritten.

Two sentences in the body above are superseded by that and are left
standing as the record they are. Decision 8's "recomputing the ID is not
the whole check" names "three of the four hashed inputs —
`container.digest`, `sdk.sha256` and `target.board`"; read it as **two
of the three**, `sdk.sha256` and `target.board`, with `zephyr` a fourth
declared value that no hash covers and that the same duty therefore also
has to compare. And §4's `subprocess`-profile rule — "rejects a session
whose `container.digest` it does not match" — is now "rejects a session
whose required Zephyr line its one build environment does not carry",
which is the same rule about the same thing: a backend with exactly one
build environment cannot choose, so it can only accept or refuse.
