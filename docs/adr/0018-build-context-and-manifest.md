# 0018 — The build context: self-contained, content-addressed, archivable

- Status: accepted
- Date: 2026-08-08

## Context

Part of the finalized remote-build architecture (see ADR 0017 for the
set). A build that runs somewhere else — a local container or a remote
build server — needs a defined input. Today that input is the resolved
`device-model.json` wire format (dashboard ADR 0007; `mcuhome build
--model`), which carries the device model and nothing else: no record
of the resolved versions, no integrity information, no room for the
patches a development build needs, and nothing a build could be
reproduced from years later.

The device configuration pins versions as **constraints** (`^2.3.6`,
`~2.3.6`, exact — ADR 0013's per-device pinning), which something must
resolve to exact versions at a defined moment. And reproducibility has
a boundary drawn by ADR 0015 §8: signing is detached and per-user, so
the signature can never be part of what a build environment reproduces.

## Decision

### 1. The build context is the self-contained input artifact

The context is what the lib produces and hands to a builder. It
contains everything a build needs **except** the toolchain and Zephyr
(in the builder container) and the SDK (fetched as a pinned package).
It is a plain directory, transported as an archive:

```
context/
  manifest.yaml            # the only file a builder must parse first
  model/device-model.json  # canonical device model (existing wire format)
  patches/                 # optional, dev builds only
    zephyr/0001-*.patch    # layer = subfolder, order = NNNN- filename prefix
    sdk/0001-*.patch
    chip/0001-*.patch
```

Output artifacts are never written back into the context.

### 2. A patch is just a file

Patches carry **no separate manifest section**: a patch's integrity
lives in the `files` list like every other file's, its target layer is
its subfolder, its application order is its filename prefix. A
declared patch list would be redundant on every axis — and it would be
worse than redundant on the security axis, where the rule already is
that no declared list is authoritative: server policy is re-derived
from the patch files *actually present* (ADR 0019), so a list that
could disagree with the files must not exist.

### 3. Lock semantics

The device config carries constraints; the lib resolves them **at
context creation** to exact pins — mcuhome version, container digest,
SDK package hash — and records **both intent and resolution** in the
manifest. Everything downstream of context creation operates on exact
pins only.

### 4. Content-addressed identity — and deliberately no artifact cache

The context ID is a canonical hash over the build-relevant manifest
fields (normative rule in decision 6). It serves as *identity*:

- **integrity** — the server recomputes it from received bytes and
  rejects mismatches (ADR 0019);
- **attribution** — every artifact names the effective context ID it
  was built from;
- **archival reference**.

There is deliberately **no artifact cache** in v1 (product-owner
decision). The recorded reasoning: device configs are near-unique —
hostname and device ID differ per device, and any config fix changes
the ID — so hit rates would be ~0 while every build pays the
bookkeeping. Compile-time savings come from ccache (ADR 0019), which
hits across *different* configs because the bulk of Zephyr/CHIP
objects is config-independent. Should a real artifact-cache use case
appear later (e.g. a community config registry), the content-addressed
identity is already in place — a cache can be layered on without any
protocol change.

### 5. Archivable, and extendable during a session

**Archivable.** Context + pinned container digest reproduces the build
years later. Reproducibility covers the *unsigned* image; the
signature is per-user and detached by design (ADR 0015 §8 — the key
never leaves the user).

**Extendable.** The base context can be extended mid-session (e.g.
patches created after a `verify`); the *effective* context = base +
extensions, re-hashed at build time. The lib always mirrors the
effective context locally (it created every piece), so archivability
is unchanged.

### 6. The context manifest and the normative hashing rule

```yaml
context: 1                          # manifest format version
created: 2026-08-08T10:00:00Z       # informational — never hashed
mcuhome:
  constraint: ^2.3.6                # original intent — never hashed
  version: 2.4.0                    # resolved exact pin (lib = SDK version)
  package:                          # resolved SDK package (CI-built, hash-pinned)
    url: https://…/mcuhome-sdk-2.4.0.tar.zst   # hint only — never hashed
    sha256: <hash>
container:
  image: ghcr.io/mcu-home/builder   # informational — never hashed
  tag: zephyr-4.4.0-r1              # informational — never hashed
  digest: sha256:<hash>             # THE container identity; the only hashed field
target:
  board: nrf7002dk/nrf5340/cpuapp
files:                              # integrity list: every file in the context,
  - { path: model/device-model.json, sha256: <hash> }      # patches included
  - { path: patches/zephyr/0001-fix.patch, sha256: <hash> }
id: sha256:<hash>                   # canonical hash (identity), rule below
```

**Normative hashing rule — locked in v1, can never change later.** The
context ID is computed over exactly these fields: container digest,
SDK package sha256, target board, and the file integrity list (entries
of path + sha256, sorted — patches are ordinary entries). Every listed
file contributes its own content hash; the sort only makes the
encoding deterministic. Explicitly excluded: `created`, `constraint`,
`package.url` (any source yielding the pinned hash is equivalent), and
`container.image`/`container.tag` — the digest alone identifies the
container, so a context resolved via `latest` and one resolved via the
equivalent versioned tag hash identically.

Encoding: **canonical JSON (RFC 8785)** of exactly this structure —
never the YAML file bytes, never the transport archive bytes (neither
serialization is deterministic).

Extension rule: new build-relevant fields enter the hash only together
with a `context` format-version bump.

The full normative statement, including the exact canonical structure,
lives in the builder container contract
([builder-container-contract.md](../design/builder-container-contract.md)
§4) — both sides of the contract compute the same ID.

### 7. SDK ↔ container compatibility is label-based, not tag-based

An SDK release declares a *constraint over the container coupling
labels* (`org.mcuhome.zephyr`, `org.mcuhome.toolchain`), never an
enumeration of blessed tags. The `-rN` tag suffix is a build serial
with no compatibility meaning — a CVE respin (r1 → r2, same coupling)
is picked up by resolve automatically without republishing SDK
releases. Third-party containers qualify by satisfying the same label
constraint.

## Consequences

- The device model stays what it is — one file inside the context; the
  wire format of dashboard ADR 0007 is contained, not replaced.
- Dev builds get patches as first-class, integrity-checked context
  content, with policy derived from the files themselves (ADR 0019).
- Every artifact is attributable to an exact, recomputable input
  identity, and any archived context is rebuildable against its pinned
  container digest — for the unsigned image, by design.
- The hashing rule is frozen: implementations may be rewritten, the
  rule may not. Evolution happens only via the `context` format
  version.
- Deferred, recorded so they are not lost (not decided): a generic
  hash-pinned `inputs/` section in the context for external artifacts
  (delta OTA, factory data); an artifact cache keyed by context ID
  (dropped for v1, see decision 4 — the identity needed to add one
  later already exists, so nothing is burned).
- Related standing decisions: ADR 0007 (containerized toolchain),
  ADR 0008 (Zephyr pinning), ADR 0013 (constraint-based per-device
  pinning), ADR 0015 §8 (detached signing — the unsigned-image
  boundary), ADR 0017, ADR 0019; dashboard ADR 0007 (wire content).
