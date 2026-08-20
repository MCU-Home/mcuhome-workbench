# 0019 — Session build protocol and the build-container contract

- Status: accepted
- Date: 2026-08-08
- Finalized: 2026-08-14

## Context

Part of the finalized remote-build architecture (see ADR 0017 for the
set; the input artifact is ADR 0018's build context). The layer decided
on 2026-08-08 is the **only valid remote-build concept**: the
architecture was planned once, found wanting, and re-decided, and what
is valid is that layer and nothing older — ADR 0017, ADR 0018, this
ADR, ADR 0020, the build-container contract and dashboard ADR 0012.
Everything older that concerns the build server was **dismantled, not
migrated**: dashboard ADR 0003 (two Home Assistant Apps, with "the
build server *is* the toolchain container"), dashboard ADR 0006 (the
job protocol), dashboard ADR 0011 (builder coupling), and
`docs/design/builder-pipeline.md` §5/§6. Dismantled means the text is
gone or marked superseded where it stands; concepts were not carried
into the new protocol and renamed.

What the older layer *does* hand forward is stated by dashboard
ADR 0012, not by dashboard ADR 0006 surviving as a standing decision:
WebSocket plus bearer token, TLS at the deployment, the leaked-token
threat model, the `_mcuhome-build._tcp.local` mDNS discovery; "the
dashboard never compiles"; and user key handling with detached signing
on the dashboard side.

The shipped protocol that this ADR replaces (dashboard ADR 0006) was
job-based: one shot, one build, artifacts, done. Three needs outgrow
it: **correctness** — state produced by earlier steps (prepared
workspace, applied patches, unpacked SDK) must survive into later
steps, and with digest-pinned containers the *version* race is already
impossible while the *state* race is not; **the dev loop** — warm
incremental rebuilds; **extensibility** — multi-phase flows
(build → test → package) without protocol redesign.

At the same time the build container (ADR 0007) becomes a replaceable
part: any container satisfying a published contract is a usable build
environment — including third-party ones with their own toolchains,
even NCS — transparently under the same infrastructure.

Terminology, fixed on 2026-08-09 and load-bearing throughout: the
build environment is the **build container** and the orchestrator is
the **build server**; "builder" is retired as a term, and the
normative document is
[`build-container-contract.md`](../design/build-container-contract.md).
"The lib" of the first drafts means the package set of ADR 0020. This
ADR's title carries the corrected term; its filename deliberately does
not, because an ADR's number is its identity and a rename would break
every cross-reference already pointing at it.

## Decision

### 1. The unit of interaction is a session

One session = one build environment = one effective context; in the
container profile, one session = one container instance, and the
session is the trust boundary. The same verb set has two backends by
transport: **local** (the workbench drives the build environment
directly) and **remote** (the build server proxies the identical
verbs, adding auth, policy, scheduling). Transport for remote:
WebSocket plus a bearer token — dashboard ADR 0006 decisions 1–2,
adopted explicitly. The token is the deployment's requirement rather
than the protocol's: MCUHome's own build server always requires one,
because an authenticated session is equivalent to shell access, but
the protocol permits a server that wants no `Authorization` header —
the case of a server reachable only through an already-authenticated
channel — so clients treat the token as configuration that may be
absent. The remote backend's client is
`mcuhome.workbench.sessionclient`, the `remote` build method, shipped
as the workbench's optional `remote` extra (E18).

**The build server is an orchestrator, and it has two deployment
targets.** It drives build environments and is never one itself.
**Standalone and self-hosted is the primary target**: a machine an
operator installs the service on and reaches over the transport above.
The **Home Assistant App is an additional target** — not the shape the
design is drawn around — and it is served by the subprocess profile
below rather than by a second architecture. Naming which target is
primary is load-bearing: it is why deny-by-default patch policy, the
ingress caps and the whole hardening floor of decision 8 are written
for a server that strangers may reach, and it is why the reduced
guarantees of the subprocess profile are acceptable at all, since they
belong to the deployment in which operator, user and device owner are
the same person.

**Two backend profiles**, named by build environment, which is the
axis the isolation rules actually hang on (contract §1.2):

- **container** — the backend materializes one container per session.
  Every isolation guarantee of decision 8 applies unchanged.
- **subprocess** — the build environment runs **in the same filesystem
  as the build server**, but as a **separate process** (the Home
  Assistant App case). The backend runs the build program as a
  subprocess in its own filesystem namespace instead of via `docker
  exec`. Same invocation ABI, same request document, same result
  document; the context, SDK and output areas are ordinary directories
  rather than mount points — which is precisely why the ABI of
  decision 4 freezes no paths.

  What is shared here is the **filesystem, not the process**, and that
  distinction is what lets "the build server drives build environments
  and is never one itself" hold without an exception: even here the
  build server orchestrates — it materializes the paths, invokes the
  program and reads its result. Contract §1.2 states it in the same
  words. Subprocess, not literally in-process, for reasons the
  subprocess backend's own module records
  (`build-server/mcuhome/buildserver/subprocessbackend.py`): a build
  running inside the server process cannot be cancelled without
  killing the server; an out-of-memory kill or a segfaulting compiler
  takes the queue, the job history and every connected client with it;
  and only a separate process is honest about the interface. Loading
  the program into the server would additionally make MCUHome's own
  Python implementation the only implementable one, against this ADR's
  premise that a third party may ship its own, and would put
  third-party patch code in the address space of the Home Assistant
  App.

  Its reduced guarantees are named rather than implied: **no network
  isolation, no per-session resource limits, no container trust
  boundary.** "The session is the trust boundary" is a statement about
  the container profile. A subprocess backend serves exactly one build
  environment — the one it runs in — and rejects, typed, a session
  whose required Zephyr line that one build environment does not carry
  (since E61; a backend with exactly one build environment cannot
  choose, so it can only accept or refuse).

The `open-session` response declares which profile serves the session
(`negotiated.backend_profile`), because the profile decides which
promises are being made and a client must not have to infer them from
behaviour.

**The price of sessions is stated with the decision:** a session holds
resources while the client thinks — and a *busy* session is not a free
session. Servers therefore enforce leases with heartbeat, an idle
timeout, a hard TTL and a concurrent-session cap. **Limits are per
server; the per-user machinery belongs to the hosted phase.** v1.0 is
single-tenant: maximum concurrent sessions, TTL, idle timeout, disk
budget and the compile-lane limit are all **per server**, and there is
no work metering and no cost classes. The reason is that the per-user
machinery has no subject today: one bearer token is one principal, a
deployment holds one token, so a per-user quota would be a per-server
quota with a misleading name, and metering would bill one principal
for its own machine. The hosted-phase concepts — per-user
concurrent-session quota, metering of actual work (CPU-seconds / build
invocations against rolling budgets), assigned cost classes and
cost-class scheduling — are bound to the hosted phase (dashboard
ADR 0006's post-1.0 outlook) rather than implemented now. Decision 8's
hardening floor is untouched by this, and that is the point of
separating the two: it is identity-independent — ingress caps, safe
extraction, never trusting declared hashes, an operator-controlled
source list and the per-session disk quota all mean exactly the same
thing with one principal as with a thousand.

**Session end is learned, not announced.** A session ends in one of
three ways — the client closes it, the connection is lost, or the
lease expires — and a client learns of an expiry the next time it uses
the session, as the typed `session.expired` refusal. A proactive
session-scoped eviction event (so a connected-but-idle client hears of
its eviction without asking) is a real improvement and an explicit
later block; it is not v0.1, because the concurrent-session cap
already bounds what an abandoned session costs and `session.expired`
already names the condition legibly when it matters.

### 2. The verb set

Eleven verbs, and the set is append-only. The session and the build
context have different lifetimes, and the flow says where the boundary
is:

```
capabilities       pre-session; may be asked at any time
open-session       session id, lease, version negotiation (no context yet)
send-context       base context incl. the pins; the container can be chosen
extend-context     repeatable; MUST NOT touch the pin file
[read-only commands permitted]
lock-context       freezes the context, writes manifest.yaml, computes and
                   returns the context id; unlocks the working commands
verify / build     only from here
get-artifact
cancel             wherever an invocation runs
close-session
attach-session     whenever a connection was lost
```

The "[read-only commands permitted]" line names, today,
`capabilities`, `attach-session`, `close-session` and `cancel`; no
context-reading verb exists yet, and the line stays as the place one
would slot in. Every authenticated session verb counts as a command
for the idle timeout and refreshes it — one rule, no per-verb list.

- **`capabilities`** — pre-session, cheap, unmetered: protocol
  version, available builder images (tag + digest + contract labels),
  per-layer patch policy, session quota. The workbench consults it
  during pin resolution and fails fast ("this server has no build
  container for zephyr-4.4.0") instead of dying mid-session. The
  answer also **announces the ingress caps** (E57): an `ingress`
  object carrying the five caps of decision 8 — read from the server's
  own configuration, never from a constant, because the config is the
  policy, and under the key names the first client already read — its
  documented forward-guess became the shape — plus `frame_bytes`, the one transport bound that lives
  below the verbs: the maximum WebSocket frame the server accepts
  (today 8 MiB), whose overrun is a dropped connection rather than a
  typed refusal and therefore must be knowable in advance or not at
  all. The caps exist so a client can refuse an oversized upload
  before the first byte leaves; a cap the client cannot see can only
  be discovered by hitting it.
- **`open-session(protocol version, context-format version,
  profile)`** — policy check + admission; three operands and no
  fourth. The response carries what **admission alone** decides:
  server protocol version (the shipped protocol is version 2),
  supported context-format range, the session ID, the lease and the
  backend profile. Version mismatch is a typed rejection at the door,
  never a downstream failure. Container materialization is **lazy** —
  the backend may defer creating the container until the first command
  that needs one. As first drafted the verb carried a "manifest
  header" operand and the whole negotiation, including the serving
  container's contract version and command set; both left when
  `lock-context` separated context from session. With no context at
  `open-session` the backend does not yet know **which** build
  container serves the session, so the discovery payload is split
  along the line the flow already draws: `open-session` answers what
  admission decides, `send-context` answers what the context
  determines. Nothing is answered twice, and what discovering early
  was for survives intact, because `send-context` precedes
  `lock-context` and therefore precedes every working command. The
  pins arrive with `send-context`, in `context.yaml` (ADR 0018 retires
  the term "manifest header" and names the pin file).
- **`send-context(archive)`** — upload the base context including the
  pins. The wire shape (E41): the payload announces the archive's
  compressed size and SHA-256, the bytes follow as WebSocket BINARY
  frames within the frame cap, and the format is **tar.zst**, fixed. A
  second base context in one session is the typed `context.exists` —
  the pins were already accepted, and replacing them mid-session is a
  new session (E43). The **response is part of the protocol** (E60),
  and it answers the container half of discovery: `pins` is what the
  client sent — under context format 2 it carries `zephyr`, the
  required Zephyr line (the canonical model's `toolchain.zephyr_line`,
  ADR 0013), and no container block — and `container` is **this
  server's own choice** (E61). Context format 1 pinned a container
  digest instead and does not exist — nothing was published against
  it; the full format-2 rule and its reasoning are in the contract's
  §3.2, §3.3 and §11. The client pins a Zephyr line
  because that is what it can know: a client knows which Zephyr its
  device needs and cannot know which images a given server holds, so a
  client-chosen digest would be either a guess or a round trip the
  protocol would have to keep offering forever. The product owner's
  correction that settled it: the digest is pinned only by the server
  itself; the client pins a Zephyr version, the server uses a matching
  container and writes the exact digest into the manifest — or answers
  with an error when it cannot satisfy the pin. The answer's
  `container` object carries `image`, `tag` and `digest` — the three
  names `manifest.yaml` records, so what a client reads here and off
  the manifest are the same fields — beside the `contract`, `program`,
  `version` and `actions` fixed by E60, answered out of the image's
  `describe`, which is authoritative, rather than out of the labels,
  which are a pre-start hint cross-checked against it. `digest` may be
  `null`, for an image the server built locally and never pushed: such
  an image names no fetchable bytes, and saying so is the honest
  reading of a field whose names are now promises. An unsatisfiable
  line is the typed `version.builder-unsatisfiable`, raised here,
  before the context is frozen — a new registry entry beside
  `version.builder-unavailable` rather than a widening of it, because
  the two answer different questions and a client can act on exactly
  one of them: `builder-unavailable` is about *one* image and is
  actionable only by the server's operator, while this one is about
  the server's whole inventory and carries `required` (the line) and
  `available` (the lines actually served) — enough for a client to
  choose another server or another `zephyr_version` without anybody
  touching that host. Neither is retryable; nothing is pulled.
- **`extend-context(files)`** — per-layer **replace semantics** (add /
  overwrite / remove), repeatable. It MUST NOT touch the pin file:
  before the lock there is no `manifest.yaml` at all, `context.yaml`
  is what may not change (the typed `context.pins-immutable`), and
  after the lock the context is closed to writes entirely. After every
  extension the server re-derives the patch-layer set from the files
  *actually present* and re-runs policy — patch semantics live
  entirely in the paths (ADR 0018 decision 2); there is no declared
  patch list that could disagree.
- **`lock-context`** — freezes the context, writes `manifest.yaml`,
  computes and returns the context ID; unlocks the working commands.
  The alternative was an implicit freeze on "the first writing
  command", rejected for a structural reason: it needs an enumerated
  list of writing commands, and that list has to be kept in sync with
  a verb set that is append-only by decision — a third-party command
  could not know which side of the line it falls on. The explicit verb
  buys three more things. The context ID gets an **observable
  moment**, at which both sides compare values they computed
  independently (contract §3.3). It makes `verify` meaningful, because
  there is a stable `files` list to check against. And it yields clean
  typed errors — `context.locked`, `context.not-locked` — instead of a
  command that quietly means something different depending on what ran
  before it.

  The wire shape is minimal (E37): the request carries `session_id`
  and nothing else, the response carries the context ID and nothing
  else. The richer alternative — the client sending its own
  independently computed ID for the server to compare — was considered
  and rejected in favour of the smallest possible protocol surface.
  The comparison therefore happens **on the client**: the workbench
  computes the ID from the bytes it sent, compares it against the ID
  the response carries, and closes the session on a disagreement. The
  consequence is stated so nobody rediscovers it as a gap: the server
  never sees the client's value, so the server cannot raise a
  mismatch — holding the workbench to its comparison duty is a
  requirement on the session client (the `remote` build method), not
  on the protocol, and the conformance obligation belongs in the
  client's own suite.

  Freezing is the server's act (E7): the freeze writes
  `manifest.yaml`, and since E61 that includes the chosen container
  (`image`, `tag`, `digest`) — outside the ID by construction, which
  is what lets two servers serving one Zephyr line freeze the same
  bytes to the same identity while each manifest records, exactly,
  what built there. A context that was sent but is empty may be
  locked: it has a well-defined ID, and the things a `build` needs
  beyond existence — `keys/signing.pub` above all — are checked by
  `build`, which is where the contract scopes them, not by the lock.
- **`verify`** — deep in-session assertion that the materialized
  context is the context the manifest describes: the program
  recomputes the effective context ID from the materialized files and
  reports it (contract §7.3); a tampered or incomplete context is one
  typed answer naming the offending paths. Optional; skipped on the
  fast path, and meaningful at all only from the lock onwards, because
  the lock is what yields the stable file list.
- **`build [mode]`** — `clean` (fresh workspace; the default and the
  safe mode — it never silently reuses state, and it is what
  release/OTA artifacts require for reproducibility and attribution;
  the first build in a freshly materialized container counts as clean)
  or `incremental` (warm workspace — the dev iteration loop; results
  are session-private, and a session with no prior state falls back to
  clean; the script "DEV mode" this originally anticipated was
  overtaken by ADR 0020's build methods). Every command invocation gets a server-assigned
  **invocation ID**; outputs land in the per-invocation area — in
  protocol terms `/out/<invocation-id>/`. The invocation ID is purely
  protocol addressing: `get-artifact` and `cancel` address an
  invocation by it, and the backend never names it to the program,
  which addresses its invocation by the paths it was handed. The
  result names the effective context ID actually built, so artifacts
  stay attributable. Before each working invocation the backend
  re-checks the locked context against the pins the session was
  admitted on — the board and the Zephyr line; the container digest
  left that comparison when E61 made it the server's own record, since
  a server comparing its own choice against itself checks nothing.
- **`cancel(session_id, invocation_id)`** — aborts the running
  invocation; the session and its warm container survive. It is a
  necessity rather than a convenience in the container profile for one
  concrete reason: **killing a `docker exec` client does not stop the
  process inside the container.** A backend that merely drops the exec
  connection leaves the compile running and the session's resources
  held. It is also the deliberate counterpart to `attach-session` —
  connection loss is never abandonment, so cancellation must be
  something a client *says*. The verb **acknowledges immediately**:
  the answer means "the stop signal is set", never "the invocation has
  stopped". The actual end travels on the channel that already exists —
  the invocation's event stream, and a result document whose `status`
  is `cancelled` (contract §5.4). A verb that blocked until the
  invocation stood would hang on the socket for up to the cancel grace
  period (`limits.cancel_grace_seconds`), inherit every reconnect question `attach-session` exists to
  answer, and need rules for a second `cancel` racing the first —
  three costs for a guarantee the result document already gives. Two
  edges are typed: an `invocation_id` the session does not know is the
  error `invocation.unknown`; an invocation that has already finished
  is answered `already_finished` and is **not** an error, because the
  race between a cancel and a natural completion is legitimate and
  both parties behaved correctly.
- **`get-artifact(invocation ID, path)`** — streamed as an announced
  archive whose bytes the server re-verifies at egress (contract
  §9.3); the per-file hashes a client checks are the ones the
  invocation's verdict declared. Artifacts are fetchable throughout
  the session — and only then: **context and artifacts are destroyed
  at `close-session`.** The per-session directory — the context and
  every artifact in it — is deleted at `close-session`, so artifact
  download happens inside the session, after the build and before
  closing. The first draft granted "a bounded grace period after
  close" and never bounded it; that was removed, because the directory
  it kept alive holds a device's Matter commissioning credentials
  (dashboard ADR 0007 decision 2) and is archivable by design
  (ADR 0018 decision 5) — an unbounded retention of exactly that
  material is the wrong default to leave standing in a protocol whose
  evolution is append-only. Recorded direction, confirmed by the
  product owner and explicitly **not** a v1.0 requirement:
  commissioning credentials are to move out of the build entirely —
  injected into the image locally after the build, exactly as the
  signature already is (ADR 0015 decision 8) — so that neither build
  server nor build container ever receives them (dashboard ADR 0007
  decision 5). The consequence for design work now is narrow and
  binding: nothing may entrench credential handling on the build side.
- **`attach-session(session ID)`** — **connection loss is not
  abandonment**: a running build continues detached (heartbeat grace
  period generous relative to build duration, bounded by the TTL);
  events are buffered server-side with sequence numbers so a
  reconnecting client resumes streams from an offset. Replay is
  literal from `from_seq` (E59): the server keeps no per-client
  delivery ledger, exactly as `lock-context` keeps no per-client
  comparison. A client that asks low legitimately sees events again —
  that is what makes the replay a debugging tool — and the client that
  wants exactly-once folds duplicates by the highest `seq` it has seen
  per invocation: minimal server, a stated client duty, conformance
  owned by the client's suite. The idle timeout counts absent
  *commands*, not absent connections.
- **`close-session`** — reap the container; leases and timeouts
  guarantee reaping when a client truly vanishes. On a busy session it
  **cancels implicitly**: the running invocation receives the cancel
  signal, its result document is still written, then the session is
  reaped. Refusing to close while an invocation runs was rejected
  because connection loss is never abandonment (that is
  `attach-session`'s reason to exist), so closing must never require a
  live client to first cancel, reattach, or wait — a crashed client's
  session would otherwise hold its resources until lease expiry as the
  *normal* path rather than the fallback.

**Session profiles** (`oneshot` | `dev` | `test`) are named at
admission and echoed in the `open-session` answer. As shipped they are
an admission label and nothing more: every session gets the same
per-server lease regardless of profile, and no verb is
profile-restricted. Per-profile budgets and a typed profile violation
are **not** part of v0.1; they are a named later block, to be designed
if a deployment ever needs a session class with its own limits. The
reasoning stands either way: a profile that promised a budget the
server does not keep would be the "accept the job and quietly do
something else" failure at the session layer.

**Fast path:**
`open-session → send-context → lock-context → build → get-artifact →
close-session`; the workbench offers a one-shot `build` convenience
that does exactly this. The lock is on the fast path by construction —
a client that never locks the context can never reach `build`.

### 3. Events, errors, results

Events: per command, a typed progress stream plus a separate raw log
stream. On the wire there are four frame kinds — `result`, `error`,
`event`, `log` — all invocation-scoped; the raw log is its own kind
rather than an event with a name (E46), because contract §8 makes the
two different things: events are a typed, registered, append-only
vocabulary a consumer matches on, while stdout and stderr together are
one raw, opaque stream. The server's completion verdict is the
**`invocation.verdict`** frame (E58), carrying the status and the
artifact list. It was first given the same name as the contract's §8
program event `invocation.finished`, distinguishable only by carrying
no `seq` — so a program that violated §8 by omitting `seq` would have
had its own announcement read as the server's verdict. The contract is
frozen and keeps its event name; the session layer was renamed while
renaming cost nothing, and the discrimination is now structural, not
the absence of a field. No session-scoped event frame exists yet; the
lease-warning and eviction events of the first draft are **not**
emitted — session end is learned, not announced (decision 1) — and
neither are the queue-position/ETA events the first draft sketched: a
queued invocation is silent until it starts (deferred, not
superseded).

Errors: fixed envelope `{code, layer, retryable, message, details}`.
Codes come from an **append-only** dotted registry; `retryable` is
authoritative (clients never infer it); unknown codes are treated as
non-retryable-fatal with the message surfaced. The registry's layers
as built are `policy.*`, `session.*`, `context.*`, `version.*`,
`builder.*`, `invocation.*`, `sdk.*`, `artifact.*`, with `x-*`
reserved for third parties. The `builder.*` prefix deliberately
survives the retirement of "builder" as prose: a prefix is a wire
value that clients match on rather than a word a reader interprets,
and as the registry records, it names the *role* — the thing that
builds, whatever shape it takes — which in the subprocess profile is
no container at all.

Result payload (per `build`): the result document declares the
artifact list with per-file hashes — the unsigned `firmware.hex` /
`firmware.bin`, their companions, and `build-report.json` — plus the
effective context ID actually built and, for every patched layer, the
applied patch-set identity (contract §5.4). The build report exists
for one consumer and was cut to what that consumer needs: the client
that signs detached, which is the only party holding the private key —
it carries the `imgtool` signing arguments and the linker's memory
table (contract §7.2.1). Signing happens client-side (dashboard/CLI,
detached imgtool, ADR 0015 decision 8) — unchanged. Values the backend
computes itself — the container digest, the effective context ID — the
backend also records itself, in `manifest.yaml` and its verdict,
rather than trusting the program's echo.

### 4. The build-container contract v1 — frozen ABI

Any container satisfying the contract is a usable build environment.
The normative specification is
[build-container-contract.md](../design/build-container-contract.md);
it doubles as the future public "bring your own build container" spec,
which is why its ABI is frozen deliberately. The load-bearing points:

- **The invocation (frozen in contract v1):** the container starts
  idle and stays alive for the session; each command is executed as
  `/mcuhome/run <action> <absolute path of the request document>` —
  two positional operands, never a flag. The entry point was first
  drafted as a `mcuhome-builder <command> /ctx --out /out` multiplexer
  on `$PATH` with parameters at a fixed path inside the context; every
  part of that was replaced before the surface was signed. A fixed
  absolute path rather than a bare name, because the image author
  controls `PATH`, `docker exec` inherits the environment fixed at
  container creation, and the name resolves without a shell — a bare
  name is a promise about a filesystem MCUHome does not control.
  `/mcuhome/` reserves a namespace in the image and carries the brand;
  the filename names the role rather than the action, because the
  action is an argument and cannot also be the name; and there is no
  extension, because a third party may ship a compiled binary and
  `.sh` would then be a lie. Precedent for both halves: Cloud Native
  Buildpacks (`/cnb/lifecycle/*`, `bin/detect`, `bin/build`) and Dev
  Container Features (`install.sh`) each fix an absolute path, and
  neither brands the executable with the vendor's name.
- **Everything else travels in the request document**, which lives in
  a backend-owned per-invocation directory — never inside the context.
  That makes the context a genuinely read-only input, and it removes
  the race a fixed in-context path would have, where two concurrent
  execs overwrite each other's document.
- **The contract defines no mount points** (contract §4 names trees,
  not paths): freezing `/ctx`, `/sdk`, `/out` would have frozen
  MCUHome's own topology — and in the subprocess profile they are
  ordinary directories, not mounts. **The program assembles its own
  build environment**: west workspace, module registration,
  `ZEPHYR_BASE`, `CHIP_ROOT` — the program builds them from the trees
  it is given, and the backend never supplies a workspace. A set of
  paths is not a build: the generated CMakeLists resolves
  `${ZEPHYR_MCUHOME_MODULE_DIR}` and searches for `CHIP_ROOT` next to
  `ZEPHYR_BASE` (`mcuhome/compiler/generate.py`), and neither exists
  outside a registered Zephyr module tree. Assigning the
  responsibility is what makes the contract implementable by an image
  with a different topology — an NCS one, say; freezing topology
  fields would have frozen MCUHome's own layout instead.
- **Exit codes 0 / 1 / 66, frozen.** `0` — the invocation ran and the
  work succeeded; a result document exists. `1` — the invocation ran
  and the work did not succeed; a result document exists. `66` — the
  request was unusable and no result could be addressed; no result
  document. Anything else means the program died, and stays undefined
  forever. The first draft reserved `64` = unsupported command and
  `65` = unsupported required parameter; both were dropped before
  anything deployed. They are `EX_USAGE` and `EX_DATAERR` from BSD
  `sysexits.h`, which foreign runtimes emit for ordinary argument
  errors, so a Go program returning 64 on a typo would be read as
  "command not supported" and have its work rescheduled. Why a command
  was refused is an enumerable and growing list, which by this ADR's
  own evolution rule makes it document content rather than a frozen
  number — it lives in the result document's `reason`
  (`unsupported.action`, `unsupported.required`, …), read whenever a
  result document exists.
- **Result and events:** machine result at the path the request
  document names, versioned (`result: 1`) with an enumerated `status`
  set (`success` | `failure` | `unsupported` | `cancelled`). The
  program emits NDJSON events on the channel of contract §8; the
  backend relays them as typed events. Raw logs are a separate stream.
- **Network: none during the build.** Backend-enforced in the
  container profile; in the subprocess profile there is no namespace
  to take the network away from, so it is an obligation on the program
  (contract §9.1). Everything a build needs is provided.
- **Labels:** `org.mcuhome.contract=1`, `org.mcuhome.zephyr=<release>`,
  `org.mcuhome.toolchain=<id>` — the server-facing, pre-start source
  for capability and compatibility checks (the client-facing source is
  the `send-context` response). The labels are a hint the backend
  cross-checks against `describe`, which is authoritative. There is
  deliberately no commands label: the first draft's
  `org.mcuhome.commands` was cut as a non-authoritative duplicate of
  `program.actions`, which is the one declaration of the action set.
  The backend loses the ability to pre-filter images by action set,
  which costs nothing: every conforming program implements `describe`,
  `verify` and `build` (contract §7), and any action beyond those
  three needs `describe` to be trusted anyway.

**The frozen surface was cut before signing.** Six items were removed
from the contract because they carried nothing, and nothing in this
ADR depends on any of them: `error.code` and `error.layer` beside
`reason` (one value in three frozen places — a backend embedding a
result into decision 3's envelope derives the envelope's fields from
`reason`, and how it does so is the backend's business rather than
contract text); `artifacts[].size` (decision 8 never trusts a declared
size on ingress and the contract's egress hardening caps sizes from
the bytes it enumerates — the declared number had no reader on either
side); the request document's `invocation` field (the program had no
use for it beyond echoing it back; the **server-assigned invocation ID
stays** as protocol addressing, and the backend never had to name it
to the program); `org.mcuhome.commands` (above); the separate
`message` field beside `error.message` (two untrusted-text fields with
one handling rule); and `layers[].count` (derivable from the patch
files the backend already holds). No capability is lost by any of the
six, and each one removed is one thing fewer for a third-party
implementer to get right.

**Why the shape is this small, restated because it is the frozen
part.** The entry point exists so a user can build and run a
completely own build container and still work inside MCUHome's system.
Extensibility therefore rides on the JSON document and never on new
argv parameters: a new parameter breaks every third-party container
that does not know it, while a new JSON field is simply ignored by an
older one. This must never change again.

### 5. Patched layers: writable views, applied once

The pristine trees are the deliberate baseline. For a layer that
carries patches, the program is handed a *writable view* of that
layer, applies the context's patches to it, and builds against it. The
contract guarantees the *behavior* — a writable view — never the
mechanism (contract §6.2), and the mechanism differs by profile:

- **Container profile: the writable view is the container layer.** The
  image's trees are already CI-patched at image build (the baked west
  workspace applies `patches/` — that is what the image *is* since
  image revision r3), and inside the container they are writable
  through the container's own copy-on-write layer. One session is one
  container, and the container is discarded at `close-session` — so a
  context-patched `zephyr` never outlives the session that patched it,
  which is the entire isolation the writable view exists to provide.
  The backend therefore asserts the tree writable at the path
  `describe` reported, truthfully, and constructs nothing: no overlay,
  no copy, no volume. An earlier revision had the backend construct
  overlays host-side; the product owner's observation that dissolved
  it — the container layer already is the overlay — simplified the
  backend to nothing.
- **Subprocess profile: host-side construction remains real exactly
  where a container layer does not exist.** The build environment is
  persistent, so the backend supplies the view — overlay (lowerdir =
  pristine source, upperdir = per-session scratch) or plain copy for
  small layers. Where an overlay is used, it is constructed by the
  backend, never mounted from inside the build environment:
  in-container mounts would require mount privileges (CAP_SYS_ADMIN)
  in the exact process that executes untrusted patch code. The
  persistence of the environment is also why patches in this profile
  are opt-in at the operator's own risk (E11, decision 7), and why
  contract §6.2 states the two profiles separately.

**Patches are applied once per session — there is no layer reset.**
The first draft obliged the backend to reset a layer's view when that
layer's patch set changed between invocations, so that a v1/v2 patch
mixture could not be attributed to the v2 context hash, and obliged
the program to record the applied patch-set identity per layer and
reapply on mismatch. `lock-context` made the triggering condition
unreachable: patches can only arrive before the lock, no working
command runs before the lock, so the patch set is constant for the
whole life of a locked context. Three obligations went away with it —
the backend's reset duty, the program's compare-and-reapply loop
(collapsed to apply once per session), and the per-tree `generation`
counter once proposed for detecting a reset.

**Interrupted patch application** — a crash, a cancellation or an
out-of-memory kill after some patches but before all — fails typed and
poisons the session: `session.poisoned` for every further working
command. The session is deliberately **not** reaped on the spot:
`get-artifact` and `close-session` stay permitted, because the moment
a session poisons is exactly the moment its owner most wants the logs
and partial artifacts that explain what happened, and destroying them
to simplify the state machine would trade diagnosis for tidiness.
Cleanup happens where it always happens — `close-session` or lease
expiry. The client's remedy is a **new session**, and therefore a
fresh build environment with pristine trees. A retry buys nothing: the
patch set is frozen, so a broken patch fails identically, and a
recovery mechanism would be two frozen contract obligations for a case
a new session already resolves. What makes the failure terminal rather
than recoverable in place is a copy-on-write fact: a pristine reset is
not possible from inside the merged view, because deleting a file
there creates a whiteout instead of restoring the base.

The `local-dev` build method has no context-patch step at all, by
design rather than by gap: the developer's own west workspace is the
build environment, and its patches are applied by the developer's
hands — a locally modified Zephyr to chase a bug or work on MCUHome's
core is the reason the mode exists. The workbench's `local-dev` method
builds what is there and attributes it accordingly.

The writable-view primitive stays reusable for other read-only bases:
an overlay over the RO ccache was considered and parked — the
ccache-native RO-secondary setup (§6) remains the simpler default.

### 6. ccache

Shared servers offer the shared cache as ccache **secondary storage**,
read-only (the session-local primary stays writable and is discarded
with the session) — jobs benefit from the warm cache and cache within
their own build without ever writing the shared store. Own servers may
mount it writable. Warming = deliberate operator invocation with a
writable cache, **trusted contexts only (no user patches)**. In the
contract the cache is an optional path in the request document whose
writability the backend asserts rather than the program probing it
(contract §10, §4.1); absent, the cache lives in the build environment
and dies with the session.

### 7. Server policy: the patches config IS the policy

No dev/strict modes. The server's configuration is the policy (E44);
unlisted patch layers are **denied by default**. As built, the policy
is exactly the operator's allowed-layer list (`--allow-patch-layer`,
empty by default), enforced against the patch files actually present
at `send-context`/`extend-context` time — never against a declared
list. Presets ship only as documented example configurations, never as
modes.

**Patches in the subprocess profile: denied by default, enablable by
the operator** (E11). The subprocess profile is not categorically
excluded from patching. The recorded reasoning is about who the two
deployments actually serve: a standalone build server may be publicly
reachable and used by strangers; the Home Assistant App variant is
normally used by one person, for their own devices, running their own
patches. Deny-by-default keeps the first case safe, and the operator
switch keeps the second usable. That it is the operator's explicit act
matters more here than in the container profile, because this is the
profile with no container trust boundary to fall back on.

### 8. Hardening floor for shared servers

All violations are typed errors.

- **Ingress caps**, enforced *streaming* during upload/extraction —
  never trusting declared sizes: max compressed size, max cumulative
  decompressed size, max entry count, max per-file size, max path
  depth. (Guards against decompression bombs filling the host before
  any build starts.) The caps are announced in the `capabilities`
  answer (decision 2, E57), so a client can size an upload instead of
  discovering a limit by hitting it.
- **Safe extraction:** only regular files and directories; reject
  absolute paths, `..` after normalization, symlinks/hardlinks/
  devices. Writes confined to whitelisted subtrees — `model/`,
  `keys/` (the signing public key is context content, ADR 0018),
  `patches/<layer>/` — plus the context's entry file `context.yaml` at
  the root, into a per-session directory the server owns.
  `manifest.yaml` is written by the backend at `lock-context` and is
  never an extraction target. (Zip-slip guard; also part of the
  container contract, so third-party tooling cannot reintroduce the
  hole.)
- **Never trust client-declared hashes:** the server recomputes every
  file hash and the context ID from the received bytes and rejects on
  mismatch — declared values are advisory. (Integrity and attribution
  requirement — independent of any caching.) **Recomputing file hashes
  is not the whole check.** Under context format 2 the ID hashes
  `sdk.sha256`, `target.board` and the file list (E61), and the
  declared values no file hash can measure are each verified where
  they become checkable: the SDK hash against the package bytes the
  backend actually fetched and unpacked, and the board and the
  required Zephyr line against the pins the session was admitted on,
  by the pre-invocation re-check — the line is what the container
  choice was made against and is hashed nowhere, so no other check on
  that path would notice it being rewritten. The container digest
  needs no comparison since E61 made it the server's own record: the
  server picks the image itself, so what used to be checked is now
  true by the way the choice is made. Without these cross-checks a
  self-consistently forged header verifies clean; the defect that
  showed it is recorded in ADR 0018.
- **SDK/container acquisition:** `package.url` is a hint only, never
  followed. The backend resolves (name, version, sha256) against its
  **configured source list** into a content-addressed, immutable,
  fetch-once store; container images are pulled only from configured
  registries. (SSRF and disk-fill guard; also makes SDK fetch
  per-session cheap.) Sources are fully operator-configurable, and the
  search order is fixed: **a local directory first, then
  `packages.mcuhome.org`, then any other external source.**

  Local first is what makes the ordinary cases work without argument.
  CI builds and hashes the `mcuhome-sdk-<version>` archive (ADR 0018
  decision 6), and a directory holding that archive is the whole first
  implementation — enough to run every build method of ADR 0020
  decision 6 before any index is published; the build server's v1
  implements exactly this first tier (E48): one or more local
  directories, searched in the order the operator listed them. A local
  directory is also the only way an unreleased or offline package is
  usable at all, so a search order that reaches the network first
  would make the development case the awkward one.
  `packages.mcuhome.org` follows as the official source, and the
  static index behind it is built as part of this work. Anything
  else — a mirror, a vendor's own server — comes last, because a
  source this project does not operate should never shadow one it
  does. Users providing their own SDK packages is an **explicit
  goal**, not a tolerated side effect: the same list that lets an
  operator point at a mirror lets a user point at a package they built
  themselves, and the "bring your own build container" premise of
  decision 4 would be worth little if the SDK inside that container
  could only come from one host. The decisive point is only *who*
  configures the list: the backend operator, never the client
  manifest. Locally the user IS the operator, so local builds can use
  any source they like. The order changes nothing about identity:
  `package.url` stays a hint that is never hashed (ADR 0018 §6), so
  the same context resolved from any source has the same context ID —
  the sha256 is what must match; where the bytes came from is not part
  of what a build reproduces.

  **On the remote path the SDK is resolved by version and guarded by
  hash, with no announcement** (E65). The client states the SDK
  version and its sha256 in `context.yaml`; the server resolves the
  version against its own sources and *verifies the bytes it found
  against the pinned hash*. A package it cannot find and cannot fetch
  is a typed refusal (`sdk.unavailable`, not retryable); a package it
  found whose bytes hash to something else is the **same** refusal,
  naming both values. The obvious symmetry — ask the server what it
  holds, then pin it — was ruled out for the reason E61 gives and one
  more of the product owner's own: with external or private registries
  the same version number can mean different bytes, and a server that
  quietly used its own copy would build against a different SDK than
  the identity claims. That must be an error immediately, never a
  silent wrong build. The hash is what makes it one — and because it
  is a *check* rather than a negotiation, asking in advance buys
  nothing: a client that announced and agreed would still have to
  verify, and a client that verifies does not have to announce. The
  client's pin source is its own: today the local SDK source
  directories the `local` method already reads
  (`--sdk-source`/`MCUHOME_SDK_SOURCE`, the first tier of the search
  order); later the registry index. Both container-shaped build
  methods therefore resolve the same pin with the same resolver before
  they create a context, which is what makes the client's pin and the
  server's check statements about one rule rather than two.
  `package.url` stays empty for a local source — a `file://` URI of
  the source directory would carry the creator's local filesystem
  layout, home directory and username into a document that is uploaded
  to a build server and archivable there. The field is filled only
  when a resolution really comes from a registry with a public
  location, where it serves the human reproducing a build years later
  and never a backend. The server accepts both never-hashed fields
  empty for the same reason: an empty `mcuhome.constraint` is
  PEP 440's own any-version specifier, and forcing either non-empty
  made the reference client invent values (contract §3.2 says so
  normatively).
- **Per-session disk quota** on the session's workspace and output
  area (typed quota-exceeded instead of host exhaustion).

Isolation rules, container profile: one **session** = one ephemeral
container instance (the session is the trust boundary), no network
during build, resource limits per session, shared ccache never
writable by jobs. Session control: lease/heartbeat, idle timeout, hard
TTL and the concurrent-session cap — per server (decision 1).

## Consequences

- Dashboard ADR 0006's transport and threat-model decisions carry
  forward — the carry-forward stated by dashboard ADR 0012, not by
  ADR 0006 surviving as a standing decision; its job-frame vocabulary
  and `GET /capabilities` endpoint are replaced by the session verbs
  above. The repository consequence (the build server leaving the
  dashboard repo) is dashboard ADR 0012.
- The container contract is a public commitment in the making: its
  ABI, exit codes, result format and label set can only be extended,
  never changed — extensions ride on new actions (advertised in
  `program.actions`) and on format-version bumps, exactly like the
  context hash rule of ADR 0018.
- Both error-code registry and verb set are append-only; clients treat
  unknown codes as fatal and never infer retryability. Protocol
  evolution has one place to happen: the `open-session` negotiation,
  together with the container half of the discovery payload in the
  `send-context` answer.
- A local build and a remote build are the same protocol, so every
  feature (patches, incremental builds, profiles) exists exactly once
  and works identically in both.
- Shared servers get a defined security floor rather than advice:
  policy is configuration, violations are typed, and nothing the
  client declares is trusted.
- Sessions hold resources; the per-server lease/timeout machinery of
  decision 1 is not optional hardening but the cost of the model,
  recorded with it.
- Deliberately deferred, recorded here so they are not rediscovered as
  gaps: per-profile session budgets and the typed profile violation; a
  proactive session-scoped eviction event; the per-user quota,
  metering and cost-class machinery of the hosted phase;
  `packages.mcuhome.org` as the second SDK source tier.
- Related standing decisions: ADR 0007 (the container this contract
  generalizes), ADR 0013 (build profiles; draft), ADR 0015 decision 8
  (client-side signing; draft), ADR 0017, ADR 0018, ADR 0020 (the
  package set and the build methods); dashboard ADR 0006 (transport,
  historical), dashboard ADR 0007 (wire content), dashboard ADR 0012,
  dashboard ADR 0013 (the dashboard building over the workbench API).
