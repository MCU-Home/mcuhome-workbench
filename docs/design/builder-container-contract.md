# MCUHome Builder Container Contract — v1

> **Status: normative, approved by the product owner (2026-08-08).**
> This document specifies contract version 1 between an MCUHome build
> backend and a builder container. Any container satisfying it is a
> usable builder — including third-party containers shipping their own
> toolchains. Rationale and the surrounding protocol are recorded in
> ADR 0018 (build context) and ADR 0019 (session protocol); this
> document stands alone and is intended to become the public
> "bring your own builder" specification.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be
interpreted as described in RFC 2119.

## 1. Terms

- **Builder container** ("the builder"): a container image providing a
  build environment (toolchain, Zephyr, and any other source trees it
  builds against) plus the `mcuhome-builder` command multiplexer.
- **Backend**: the software driving the container runtime — either a
  local library driving Docker/Podman directly, or a build server. The
  backend owns everything outside the container: mounts, views,
  network isolation, resource limits.
- **Session**: the lifetime of one container instance. One session is
  bound to one build context (as admitted); commands within a session
  share container state.
- **Build context** ("the context"): the self-contained input
  directory described in §3.
- **Invocation**: one command execution inside the session, identified
  by a backend-assigned **invocation ID**.
- **Layer**: a source tree the builder builds against, addressable by
  name for patching (§6). Contract v1 defines the layer names
  `zephyr`, `sdk` and `chip`.

## 2. Container image requirements

### 2.1 Labels

The image MUST carry these labels:

- `org.mcuhome.contract=1` — the contract version this document
  specifies.
- `org.mcuhome.zephyr=<version>` — the Zephyr version the image
  builds against.
- `org.mcuhome.toolchain=<id>` — the toolchain identity.
- `org.mcuhome.commands=<comma-separated list>` — the commands the
  builder implements; MUST include `verify` and `build`. Additional
  commands (e.g. `test`) are advertised here.

The labels are the server-facing source of truth for capability and
compatibility checks. Compatibility between an SDK release and a
container is declared as a constraint over the coupling labels
(`org.mcuhome.zephyr`, `org.mcuhome.toolchain`), never as an
enumeration of image tags: a tag or tag suffix carries no
compatibility meaning. Third-party containers qualify by satisfying
the same label constraint.

### 2.2 The multiplexer

The image MUST provide an executable `mcuhome-builder` (script or
binary) on the default PATH implementing the invocation ABI of §5.
Nothing else is required of the image's contents.

## 3. The build context

### 3.1 Layout

```
context/
  manifest.yaml            # the only file a builder must parse first
  model/device-model.json  # canonical device model
  patches/                 # optional
    zephyr/0001-*.patch    # layer = subfolder, order = filename prefix
    sdk/0001-*.patch
    chip/0001-*.patch
```

- `manifest.yaml` is the entry point; a builder MUST NOT require any
  out-of-band knowledge beyond it and this contract.
- A patch's target layer is its subfolder; its application order is
  its filename. There is no patch list in the manifest — the patch set
  of a layer is defined by the files present under
  `patches/<layer>/`, applied in ascending lexicographic filename
  order (the `NNNN-` prefix convention).
- Build outputs MUST NOT be written into the context.

### 3.2 The manifest

```yaml
context: 1                          # manifest format version
created: 2026-08-08T10:00:00Z       # informational — never hashed
mcuhome:
  constraint: ^2.3.6                # original intent — never hashed
  version: 2.4.0                    # resolved exact pin
  package:                          # resolved SDK package
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
id: sha256:<hash>                   # canonical hash (identity), rule in §3.3
```

A builder MUST check the `context` format version and fail the
invocation (a `failure` result naming the unsupported version) for
versions it does not implement. Within a session, `manifest.yaml` is
immutable: the builder MAY assume it is identical across all
invocations of the session. The `files` list covers every file in the
context except `manifest.yaml` itself and the backend-written
`.mcuhome/` directory (§5.2).

### 3.3 Context identity — normative hashing rule

This rule is **locked for context format version 1 and can never
change**. New build-relevant fields enter the hash only together with
a `context` format-version bump.

The context ID is the SHA-256 hash, rendered as `sha256:<lowercase
hex>`, of the RFC 8785 (JSON Canonicalization Scheme) encoding of
exactly this JSON structure:

```json
{
  "container": {"digest": "sha256:<hash>"},
  "files": [{"path": "<path>", "sha256": "<hash>"}, …],
  "sdk": {"sha256": "<hash>"},
  "target": {"board": "<board>"}
}
```

- `container.digest` — the manifest's `container.digest`.
- `sdk.sha256` — the manifest's `mcuhome.package.sha256`.
- `target.board` — the manifest's `target.board`.
- `files` — one entry per context file, `path` and `sha256` as in the
  manifest's `files` list, sorted by `path` (ascending byte order).
  Duplicate paths are invalid. Patches are ordinary entries; every
  listed file contributes its own content hash, the sort only makes
  the encoding deterministic.

Explicitly excluded from the hash: `created`, `mcuhome.constraint`,
`mcuhome.package.url` (any source yielding the pinned hash is
equivalent), and `container.image`/`container.tag` — the digest alone
identifies the container, so a context resolved via a floating tag and
one resolved via the equivalent versioned tag hash identically.

The hash input is **never** the YAML file bytes and **never** the
transport archive bytes: neither serialization is deterministic.

The **effective context** of an invocation is the context as present
at invocation time (a backend may extend a context between
invocations); its ID is computed by the same rule over the files then
present. Implementations on both sides of the contract MUST compute
the ID independently from the bytes they actually hold and MUST NOT
trust a declared `id` value.

## 4. Filesystem interface

The backend MUST provide these mount points:

| Path | Mode | Contents |
|---|---|---|
| `/ctx` | RO | the build context (§3) |
| `/sdk` | RO | the SDK package, unpacked and hash-verified by the backend against the manifest's `mcuhome.package.sha256` |
| `/out` | RW | output tree; the builder writes only under `/out/<invocation-id>/` |
| `/ccache` | optional | shared compiler cache volume (§8) |

The read-only mounts and the image's own source trees are the
always-pristine baseline; the builder MUST NOT depend on being able to
write to them (patched layers are handled via views, §6). If
`/ccache` is absent, the cache lives in the container layer and dies
with the session.

## 5. Invocation ABI — frozen in contract v1

### 5.1 Execution model

The container starts idle and stays alive for the session. The backend
executes each command as:

```
mcuhome-builder <command> /ctx --out /out
```

This argv is frozen: it **never grows**. All parameters travel in the
command document (§5.2).

The first `build` in a freshly materialized container is a clean
build by definition (§7.2).

### 5.2 The command document

Per invocation the backend writes one JSON document to the fixed path
`/ctx/.mcuhome/command.json` before executing the command. It is one
JSON object:

```json
{
  "command": "build",
  "invocation": "<invocation-id>",
  "params": {"mode": "incremental"},
  "required": ["mode"]
}
```

- `command` — MUST equal the argv command.
- `invocation` — the backend-assigned invocation ID; also names the
  output directory `/out/<invocation-id>/`.
- `params` — the command's parameters.
- `required` — the parameter names the caller deems essential for this
  invocation.

The builder MUST ignore parameters it does not understand unless they
are named in `required`.

### 5.3 Exit codes

| Code | Meaning |
|---|---|
| 64 | unsupported command |
| 65 | unsupported parameter that was named in `required` |

These two codes are reserved by the contract. For any executed
command, success or failure of the *work* is reported in the result
document, not the exit code.

### 5.4 The result document

The builder MUST write a machine-readable result to
`/out/<invocation-id>/result.json`:

```json
{
  "result": 1,
  "status": "success",
  "context": "sha256:<effective context id>",
  "artifacts": [
    {"path": "firmware.hex", "sha256": "<hash>"}
  ]
}
```

- `result` — the result format version; this document defines
  version 1.
- `status` — one of the enumerated set `success` | `failure`.
  Consumers MUST treat unknown values as `failure`. On `failure` the
  document SHOULD carry a `message`.
- `context` — for `build`: the effective context ID actually built
  (§3.3), computed by the builder from the context as materialized.
- `artifacts` — the produced artifacts, paths relative to
  `/out/<invocation-id>/`, each with its SHA-256. Backends serve
  artifact downloads verified against these hashes.

New fields may be added to the result document; consumers MUST ignore
fields they do not understand. Incompatible changes require a `result`
format-version bump.

## 6. Patched layers: writable views

A layer that carries patches (files under `patches/<layer>/`) needs a
writable source tree; the pristine baseline must remain restorable.
The contract guarantees the **behavior**, never the mechanism:

- The **backend** MUST provide the builder with a *writable view* of
  each patched layer, presented at the layer's canonical location (for
  the `sdk` layer that is `/sdk`; for image-internal layers such as
  `zephyr` and `chip`, the location the image builds them from). The
  reference mechanism is a copy-on-write overlay constructed
  **host-side by the backend** (lowerdir = the pristine RO source,
  upperdir = per-session scratch on the host). Copying the layer into
  a writable location is a conforming fallback.
- The overlay, where used, MUST be constructed outside the container.
  A container that executes untrusted patch code MUST NOT hold the
  mount privileges (CAP_SYS_ADMIN) that in-container overlay mounting
  would require.
- **Layer reset is the backend's responsibility.** When a layer's
  patch set changes between invocations, the backend MUST restore that
  layer's view to the pristine baseline before the next command —
  overlay: discard the upperdir; copy: re-copy. Note that a pristine
  reset is not possible from inside a merged overlay view (deleting a
  file there creates a whiteout instead of restoring the base), which
  is why reset sits with the backend.
- The **builder** MUST apply the patches of each patched layer to its
  view in the order of §3.1 before building, MUST record the applied
  patch-set identity per layer (e.g. a hash over the layer's patch
  files), and on any subsequent invocation MUST reapply the patches
  whenever the view is pristine or the recorded identity does not
  match the patch set present in `/ctx`. A build MUST never run
  against a view whose applied patches differ from the current
  context's patch set — this keeps every artifact attributable to the
  effective context ID that names those patches.

Incremental build state survives only for untouched layers.

## 7. Commands

Contract v1 defines `verify` and `build`. A builder MAY implement
further commands and MUST advertise its full set in the
`org.mcuhome.commands` label.

### 7.1 `verify`

Asserts that the materialized environment matches the manifest pins:
at minimum, every file listed in `files` MUST hash to its recorded
SHA-256. Deeper environment checks are implementation-defined.
`verify` is optional for callers; a backend fast path may never invoke
it.

### 7.2 `build`

Parameter `mode`:

- `clean` — fresh workspace. Required for release/OTA artifacts
  (reproducibility and attribution). The first build in a freshly
  materialized container counts as clean.
- `incremental` — warm workspace; results are session-private.

Expected artifacts of a successful device build: the unsigned
`firmware.hex`/`firmware.bin`, the unsigned OTA payload, and
`build-report.json` (sizes, warnings, ccache statistics, the container
digest and the effective context ID actually used). The builder MUST
NOT sign images: reproducibility covers the unsigned image, and
signing is detached and client-side by design.

## 8. Progress, logs

The builder SHOULD emit NDJSON progress events on its standard output
stream, one JSON object per line:

```json
{"phase": "cmake", "current": 3, "total": 9, "message": "configuring"}
```

Raw build logs (compiler output etc.) are a separate stream and go to
standard error. Backends relay progress as typed events and logs as an
opaque stream; consumers MUST NOT parse the log stream for machine
decisions.

## 9. Execution environment guarantees

The backend MUST enforce, and the builder MAY rely on:

- **No network during commands.** The builder MUST NOT require network
  access at any point; everything a build needs is mounted or in the
  image.
- **A verified SDK**: `/sdk` content matches the manifest's package
  hash. The backend acquires the package by (name, version, sha256)
  from operator-configured sources only; the manifest's `package.url`
  is a hint, never an instruction.
- **Safe context materialization.** Whatever transport delivered the
  context, the backend MUST materialize `/ctx` using safe extraction:
  regular files and directories only; absolute paths, `..` after
  normalization, symlinks, hardlinks and device nodes rejected; writes
  confined to the context's whitelisted subtrees (`model/`,
  `patches/<layer>/`), inside a directory the backend owns. Tooling on
  either side of the contract MUST NOT reintroduce extraction of
  non-regular entries.
- **Resource limits** (CPU, memory, disk quota on workspace and
  `/out`) are the backend's to set; the builder SHOULD respect
  conventional parallelism hints from its environment and MUST behave
  correctly when limits abort a build (a partial `/out/<invocation-id>/`
  without `result.json` is defined as a failed invocation).

## 10. ccache

If `/ccache` is mounted:

- **read-only**: the builder MUST treat it as a read-only secondary
  cache (container-local primary, discarded with the session). Jobs
  benefit from the warm shared cache and cache within their own build
  without ever writing the shared store.
- **read-write**: the builder MAY use it as its primary cache.

Shared multi-user backends MUST mount a shared cache read-only for
untrusted work; cache warming is a deliberate operator invocation with
a read-write mount and trusted contexts only (no user patches).

## 11. Versioning and evolution

- The invocation argv (§5.1), the reserved exit codes (§5.3) and the
  mount points (§4) are frozen for contract v1.
- New capabilities arrive as new commands, advertised via the
  `org.mcuhome.commands` label; unknown commands exit 64.
- New command parameters are ignored unless `required` (§5.2); an
  unsupported required parameter exits 65.
- The context manifest evolves via the `context` format version; the
  hashing rule of §3.3 is locked per format version.
- The result document evolves via the `result` version; unknown fields
  are ignored, unknown `status` values read as `failure`.
- An incompatible change to anything above is a new contract version,
  declared via `org.mcuhome.contract`.
