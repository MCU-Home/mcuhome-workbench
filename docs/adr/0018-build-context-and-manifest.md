# 0018 — The build context: self-contained, content-addressed, archivable

- Status: accepted
- Date: 2026-08-08
- Finalized: 2026-08-14

## Context

Part of the finalized remote-build architecture (see ADR 0017 for the
set). A build that runs somewhere else — a local build container or a
remote build server — needs a defined input. Before this ADR that input
was the resolved `device-model.json` wire format (dashboard ADR 0007;
`mcuhome build --model`), which carries the device model and nothing
else: no record of the resolved versions, no integrity information, no
room for the patches a development build needs, and nothing a build
could be reproduced from years later.

The device configuration pins the SDK version as a **constraint** (a
compatible-release `~=2.3`, a range `>=2.3.6,<3`, or an exact
`==2.3.6`), which something must resolve to an exact version at a
defined moment. The constraint grammar is PEP 440 (decision 3, decision
E52). It is a distinct thing from
[ADR 0013](draft/0013-binary-blob-policy.md)'s per-device *Zephyr*
pinning and blob policy — an early draft of this ADR conflated the two,
and the distinction is restated where the resolver lives
(`mcuhome/workbench/resolve_pins.py`) so it stays visible.

Reproducibility has a boundary drawn by
[ADR 0015](draft/0015-update-and-partition-architecture.md) §8: signing
is detached and per-user, so the signature can never be part of what a
build environment reproduces.

Vocabulary, fixed by the ADRs this one ships with: the **build
container** is the build environment (ADR 0007); the **backend** is the
software driving it — the workbench package driving a container runtime
directly, or a build server (ADR 0019 §1); the context is created by
the **workbench** (the package set of ADR 0020). The normative
companion is
[`build-container-contract.md`](../design/build-container-contract.md)
— renamed from `builder-container-contract.md` when "builder" was
retired as a term — whose §3 is the normative statement of the format
this ADR decides.

## Decision

### 1. The build context is the self-contained input artifact

The context is what the workbench produces and hands to a backend. It
contains everything a build needs **except** the toolchain and Zephyr
(in the build container) and the SDK (a hash-pinned package fetched per
build). It is a plain directory, transported as an archive:

```
context/
  context.yaml             # the request: format version and resolved pins (§6)
  manifest.yaml            # the lock result: pins + integrity list + ID (§6)
  model/device-model.json  # canonical device model (existing wire format)
  keys/signing.pub         # MCUboot verification key (public half only)
  patches/                 # optional, dev builds only
    zephyr/0001-*.patch    # layer = subfolder, order = NNNN- filename prefix
    sdk/0001-*.patch
    chip/0001-*.patch
    mcuboot/0001-*.patch
```

`manifest.yaml` is the program's entry point: a build container must
need no out-of-band knowledge beyond it and the contract. The layer
names under `patches/` are an append-only registry owned by the MCUHome
project — contract v1 defines `zephyr`, `sdk`, `chip` and `mcuboot`,
and third-party layers carry an `x-` prefix (contract §1.1).

`keys/signing.pub` is the **public** half of the user's MCUboot signing
key (the `PUBLIC_KEY_FILE` convention of
`mcuhome/workbench/signing.py`); the private half never travels
(ADR 0015 decision 8). It is context content and an ordinary entry of
the `files` list, on exactly the terms decision 2 sets for patches: no
separate manifest section, no declared list that could disagree with
the bytes. It belongs in the identity because the public key is
compiled into MCUboot (ADR 0015 §8 — which is why key rotation is a
bootloader replacement there): two builds with different keys produce
different bootloaders, and two different bootloaders must not share an
identity. The consequence is the decision, not a side effect: **two
users with byte-identical device configurations hash to two different
context IDs.** Attribution stops encoding only *what* was built and
starts encoding *who* built it — correct, because the images genuinely
differ, but it removes cross-user sharing from the content-addressed
identity of decision 4 by construction. The key is required for
`build` and not for `verify` or `describe` (contract §3.1), and the
extraction whitelists of ADR 0019 §8 include `keys/`.

Output artifacts are never written back into the context; it is a
read-only input for the whole life of a session (contract §4).

### 2. A patch is just a file

Patches carry **no separate manifest section**: a patch's integrity
lives in the `files` list like every other file's, its target layer is
its subfolder, its application order is its filename prefix — the
whole of patch semantics lives in the paths. A declared patch list
would be redundant on every axis — and it would be worse than
redundant on the security axis, where the rule already is that no
declared list is authoritative: server policy is re-derived from the
patch files *actually present* (ADR 0019 §7), so a list that could
disagree with the files must not exist.

### 3. Constraints resolve to exact pins at context creation

The device config carries constraints; the workbench resolves them **at
context creation** to exact pins — the mcuhome version and the SDK
package hash — and records **both intent and resolution** in the
context documents (§6). Intent and resolution stay two things wherever
there are two. Everything downstream of context creation operates on
exact pins only.

**The grammar is PEP 440 (decision E52).** An SDK constraint is a
[PEP 440](https://peps.python.org/pep-0440/) version specifier,
resolved with `packaging.specifiers.SpecifierSet` — `packaging` is
already a dependency, and PEP 440 is the version grammar the Python
ecosystem already agrees on, so a caret/tilde dialect of MCUHome's own
(which is how this ADR's first draft spelled its examples — npm-style
`^2.3.6` for what PEP 440 writes `~=2.3.6`) would be one more thing to
specify, implement and get wrong. A constraint resolves to the single
**highest** available version that satisfies it, against a **local**
set of versions — for the SDK, the keys of the `index.json` a source
directory carries (`scripts/build_sdk_archive.py`); nothing is fetched
to resolve. The reference implementation is
`mcuhome/workbench/resolve_pins.py`.

**Pre-release rule.** A dev or pre-release version (`2.5.0.dev0`,
`2.5.0a1`) satisfies a constraint only when the constraint is itself a
pre-release specifier (`==2.5.0.dev0`, `>=2.5.0a1`) or pre-releases are
explicitly allowed — `SpecifierSet`'s own `prereleases` semantics. A
stable constraint such as `~=2.3` never resolves to a pre-release.

**One resolver serves every build method** (E65). Both
container-shaped methods need the pin before a context can exist,
because `mcuhome.package.sha256` is a hashed identity input: `local`
resolves it for the container it starts itself, and `remote` resolves
it for a context it sends to a build server — which then *re-resolves
the version against its own package store and verifies the bytes
against this pin*. The version is the server's resolution key; the
hash is the byte-identity guard. A same-version-different-bytes source
is a typed refusal there, never a silently different SDK.

**No container is pinned — a container is required** (E61). The
context does not choose a build environment; it states the Zephyr
release *line* its device needs, and the backend selects a container
serving that line and records which one (§6, §7). Originally the
resolution step also produced a container digest as a pin; the product
owner took that premise away, because the client is not the party that
knows which containers a backend serves — a digest chosen by the
client is either a guess or a round-trip that would have to happen
before a context could exist at all.

### 4. Content-addressed identity — and deliberately no artifact cache

The context ID is a canonical hash over the build-relevant manifest
fields (normative rule in decision 6). It serves as *identity*:

- **integrity** — the server recomputes it from received bytes and
  rejects mismatches (ADR 0019 §8);
- **attribution** — every artifact names the effective context ID it
  was built from;
- **archival reference.**

There is deliberately **no artifact cache** in v1 (product-owner
decision). The recorded reasoning: device configs are near-unique —
hostname and device ID differ per device, and any config fix changes
the ID — so hit rates would be ~0 while every build pays the
bookkeeping; and since `keys/signing.pub` entered the identity
(decision 1), the ID additionally encodes *who* built, so a cache
would be per-key even between users with identical configurations.
Compile-time savings come from ccache (ADR 0019 §6), which hits across
*different* configs because the bulk of Zephyr/CHIP objects is
config-independent. Should a real artifact-cache use case appear later
(e.g. a community config registry), the content-addressed identity is
already in place — a cache can be layered on without any protocol
change.

### 5. Archivable, and frozen by an explicit verb

**Archivable.** A locked context reproduces the build years later: the
requirement (`zephyr`) says what build environment was needed, the
manifest's `container` block records what actually answered, and the
SDK pin names the exact package bytes. Reproducibility covers the
*unsigned* image; the signature is per-user and detached by design
(ADR 0015 §8 — the key never leaves the user).

**Extendable before the lock, frozen at it.** The base context can be
extended after upload (`extend-context`, ADR 0019 §2 — per-layer
replace semantics), and extension is bounded to the phase before
`lock-context`. As first drafted this decision read "the *effective*
context = base + extensions, re-hashed at build time"; the freeze verb
of ADR 0019 replaced that with **hashed once, at `lock-context`**.
There is one moment at which the ID exists, and both sides compare
their independently computed values there (contract §3.3). The lock is
one-way (`context.locked`): the working actions `verify` and `build`
run only from the lock onwards, so the first draft's example — patches
created after a `verify` — is not reachable inside one session; adding
a patch after a verify is a new session.

The **effective context** of an invocation is the context as
materialized at invocation time, its ID computed by the same rule over
the files then present. `verify` checks the effective context — and
under the lock that is the same act as checking against the manifest,
because the manifest `lock-context` writes *is* the integrity list of
the effective context. (The pre-lock arrangement failed by
construction: a manifest immutable for the session's lifetime *plus*
mid-session extension meant every extension made verification report
files "present but not in the integrity list". The split of §6
dissolves the contradiction rather than patching it.)

Archivability is unchanged by extension: the workbench mirrors the
effective context locally — it created every piece.

### 6. The context documents and the normative hashing rule

**`context.yaml` is the request; `manifest.yaml` is the result.** This
decision originally described one `manifest.yaml` that was two things
at once: the pinning *request* — what to build, with the constraints
already resolved to exact pins — and the integrity *record* — what the
context turned out to contain and what its ID is. The two cannot stay
one file once a context is uploaded in pieces: the pins have to exist
before there is anything to hash, and the `files` list cannot be
complete until the client stops adding to it. The undefined term
"manifest header" that ADR 0019 once used for the early-arriving part
is retired outright — there is no header separate from `context.yaml`.

**`context.yaml`** is written when the base context is created and
travels with it. It carries the `context` format version, the resolved
pins, the build environment requirement, and the original intent —
and nothing that depends on the final file set:

```yaml
context: 2                          # context format version
created: 2026-08-08T10:00:00Z       # informational — never hashed
mcuhome:
  constraint: ~=2.3.6               # original intent — never hashed
  version: 2.4.0                    # resolved exact pin (the one shared SDK version)
  package:                          # resolved SDK package (CI-built, hash-pinned)
    url: https://…/mcuhome-sdk-2.4.0.tar.zst   # hint only — never hashed
    sha256: <hash>
zephyr: '4.4'                       # REQUIRED Zephyr line — never hashed
target:
  board: nrf7002dk/nrf5340/cpuapp
```

It is what carries the pins into a session, and server policy is
checked against it when it arrives (ADR 0019 §2, `send-context`). It
is immutable for the session's lifetime: `extend-context` MUST NOT
touch it — it carries the pins the session was admitted on, and
changing them is a new session, not an extension. An attempt is a
typed error (`context.pins-immutable`).

The two informational pin fields are required **keys** whose value may
be the empty string (E65): an empty `constraint` is PEP 440's own
any-version specifier — no intent was stated — and an empty
`package.url` means the package was resolved from a location with no
public name. A context resolved from a local directory records no
`file://` URI, because that would carry the creator's filesystem
layout into a document another party stores.

**`manifest.yaml`** is written by the party that locks the context —
the backend at `lock-context` (ADR 0019 §2) in a session, the
workbench's own `lock_context` (`mcuhome/workbench/contextdir.py`) for
the local methods. It is the *result* of freezing, never an input to
it:

```yaml
context: 2
mcuhome:                            # as in context.yaml
  constraint: ~=2.3.6
  version: 2.4.0
  package:
    url: https://…/mcuhome-sdk-2.4.0.tar.zst
    sha256: <hash>
zephyr: '4.4'                       # as in context.yaml — the requirement
container:                          # THE BACKEND'S RESOLUTION — never hashed
  image: ghcr.io/mcu-home/builder
  tag: zephyr-4.4.0-r7
  digest: sha256:<hash>             # null for an image that was never pushed
target:
  board: nrf7002dk/nrf5340/cpuapp
files:                              # integrity list: every content file,
  - { path: keys/signing.pub, sha256: <hash> }             # patches included
  - { path: model/device-model.json, sha256: <hash> }
  - { path: patches/zephyr/0001-fix.patch, sha256: <hash> }
id: sha256:<hash>                   # canonical hash (identity), rule below
```

The manifest repeats the pin blocks exactly as `context.yaml` states
them, rather than referring to them, so that the lock result is
readable on its own: the document that carries an identity carries the
inputs that identity was computed from. The one field that does not
travel is `created` — it dates the request, and the manifest's own
moment is the lock. The `container` block is written by the locking
party and by nobody else: it is the record of which build environment
answered the context's `zephyr` requirement (E61), with `digest: null`
for an image that was built locally and never pushed — such an image
names no fetchable bytes, and `null` says exactly that rather than
inventing a value. The exact shape of both documents, including the
one legal lexical form of every hash value, is normative in the
contract, §3.2 and §3.3.1.

**Normative hashing rule — locked with `context` format version 2, can
never change later.** The context ID is the SHA-256 hash of the
**canonical JSON (RFC 8785)** encoding of exactly this structure:

```json
{
  "files": [{"path": "<path>", "sha256": "<hash>"}, …],
  "sdk": {"sha256": "<hash>"},
  "target": {"board": "<board>"}
}
```

— `sdk.sha256` is the manifest's `mcuhome.package.sha256`,
`target.board` the manifest's `target.board`, and `files` one entry
per context file, sorted by `path` in ascending byte order of its
UTF-8 encoding. Every listed file contributes its own content hash;
the sort only makes the encoding deterministic. The hash input is
never the YAML file bytes and never the transport archive bytes —
neither serialization is deterministic. Explicitly excluded from the
hash: `created`, `mcuhome.constraint`, `mcuhome.package.url` (any
source yielding the pinned hash is equivalent), the whole `container`
block, and `zephyr`.

Extension rule: new build-relevant fields enter the hash only together
with a `context` format-version bump. The full normative statement
lives in the contract, and the golden conformance vector is pinned in
`tests_py/test_context.py`
([build-container-contract.md](../design/build-container-contract.md)
§3.3, "Context identity — normative hashing rule"; an earlier revision
pointed at §4, which is the filesystem interface and says nothing
about hashing) — both sides of the contract compute the same ID, each
from the bytes it actually holds, never trusting a declared `id`.

**Why the `container` block is outside the identity** (E61,
2026-08-11). As first decided the container digest was a fourth hashed
input. The product owner removed the premise it rested on: the client
does not pin a container at all — it states the Zephyr line, the
backend selects a container of that line once per session at
`send-context` and records which (ADR 0019). A context that identified
itself by the backend's answer would get a different identity on every
server that serves the same line with a different image, making "built
from *this* context" a claim about a machine rather than about a set
of bytes. The three-input rule above is locked to format version 2;
the four-input draft never shipped as a format anyone can hold — there
is no context format 1.

**Why `zephyr` is not hashed either.** Hashing it would be redundancy
rather than identity: the line is a property of the SDK release that
`sdk.sha256` pins, *and* a field of the canonical device model whose
bytes are an ordinary `files` entry — two contexts differing only in
their required line already have two IDs. That argument holds only
while the copies agree, and nothing about the bytes enforces it, since
`context.yaml` is outside the integrity list; the backend duty below
therefore carries the comparison explicitly.

**Neither document is in the `files` list, and for `context.yaml` that
is a statement about the hash rather than about layout.**
`manifest.yaml` is excluded structurally: it is the document that
carries the list, so it cannot be an entry in it — the implementation
refuses one and skips the file when collecting entries
(`mcuhome/model/context.py`, `mcuhome/workbench/contextdir.py`).
`context.yaml` is excluded for a stronger reason: hashing it as an
ordinary content file would readmit `created` and `mcuhome.constraint`
through the back door, and this rule excludes both from the identity
**by name**. Two byte-identical device configurations created a second
apart would then hash differently, and one configuration resolved once
under `~=2.3.6` and once under an exact `==2.4.0` would carry two
identities for one resolved pin — the two outcomes the exclusion list
exists to prevent. Nothing is lost: `sdk.sha256` and `target.board`
are two of the three inputs of the rule, the third is the `files` list
itself, and `zephyr` is covered by the redundancy argument above.

**What verification measures, and the backend duty that completes
it.** Recomputing the ID over the `files` list is **not** a complete
check of the context, and no implementation may present it as one: two
of the three hashed inputs — `sdk.sha256` and `target.board` — are
read from the declared manifest, which is itself outside the integrity
list by construction, so **a self-consistently forged manifest
recomputes to its own declared ID.** The reference implementation
(`verify_context`, `mcuhome/workbench/contextdir.py`) has exactly this
shape, and says so. Originally its docstring read as though the check
were already complete, and nothing stated who closes the gap — the
defect was found in review and both halves are fixed. The duty is
normative: the backend MUST verify `mcuhome.package.sha256` against
the package bytes it fetched and unpacked, `target.board` against the
pins the session was admitted on, and `zephyr` against the model's own
`toolchain.zephyr_line` (contract §9.1 — the comparison that makes the
redundancy argument above true rather than merely intended).
`container.digest` needs no comparison, and gets a stronger guarantee
instead: the backend *selects* the container (E61), so a container of
the wrong line cannot be reached rather than being detected after the
fact. The duty is recorded where the "never trust client-declared
hashes" floor lives, ADR 0019 §8; `verify_context` does not establish
it.

### 7. SDK ↔ container compatibility is label-based, not tag-based

An SDK release declares a *constraint over the container coupling
labels* (`org.mcuhome.zephyr`, `org.mcuhome.toolchain`), never an
enumeration of blessed tags. The `-rN` tag suffix is a build serial
with no compatibility meaning and no identity — a CVE respin (r6 → r7,
same coupling) is picked up by resolve automatically without
republishing SDK releases. Third-party containers qualify by
satisfying the same label constraint.

This decision is what made E61 possible at no design cost: the
`zephyr` line of a context matched against a container's
`org.mcuhome.zephyr` label (contract §2.1.1, §3.2) is this same
mechanism, used by the backend — the party that was always in a
position to use it, because only the backend knows what it serves.

## Consequences

- The device model stays what it is — one file inside the context; the
  wire format of dashboard ADR 0007 is contained, not replaced.
- Dev builds get patches as first-class, integrity-checked context
  content, with policy derived from the files themselves (ADR 0019 §7).
- Every artifact is attributable to an exact, recomputable input
  identity, and any archived context is rebuildable against the build
  environment its manifest records — for the unsigned image, by
  design.
- The identity is per-key as well as per-config (decision 1): it
  encodes who built, which is truthful about the differing bootloaders
  and rules out cross-user artifact sharing by construction.
- The hashing rule is frozen: implementations may be rewritten, the
  rule may not. Evolution happens only via the `context` format
  version, and format version 2 is the first that exists.
- Deferred, recorded so they are not lost (not decided): a generic
  hash-pinned `inputs/` section in the context for external artifacts
  (delta OTA, factory data); an artifact cache keyed by context ID
  (dropped for v1, see decision 4 — the identity needed to add one
  later already exists, so nothing is burned).
- Related standing decisions: ADR 0007 (containerized toolchain, the
  container side of E61), ADR 0008 (Zephyr pinning),
  [ADR 0013](draft/0013-binary-blob-policy.md) (binary-blob policy,
  build profiles, and per-device Zephyr pinning — *not* the
  SDK-constraint grammar, which is PEP 440 per decision 3),
  [ADR 0015](draft/0015-update-and-partition-architecture.md) §8
  (detached signing — the unsigned-image boundary), ADR 0017, ADR 0019,
  ADR 0020 (which packages compute the ID, and why both sides share one
  implementation of the frozen rule); dashboard ADR 0007 (wire
  content).
