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

The device configuration pins the SDK version as a **constraint** (a
compatible-release `~=2.3`, a range `>=2.3.6,<3`, or an exact `==2.3.6`),
which something must resolve to an exact version at a defined moment. The
constraint grammar is PEP 440 (the amendment below, decision E52); it is
a distinct thing from ADR 0013's per-device *Zephyr* pinning and blob
policy, which an earlier draft of this ADR conflated it with. And
reproducibility has a boundary drawn by ADR 0015 §8: signing is detached
and per-user, so the signature can never be part of what a build
environment reproduces.

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
lives in the build-container contract
([build-container-contract.md](../design/build-container-contract.md)
§3.3, "Context identity — normative hashing rule") — both sides of the
contract compute the same ID. (Corrected 2026-08-09: this pointed at
§4, which is the filesystem interface and says nothing about hashing;
and the document was renamed with the term, see the amendment below.)

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
  ADR 0008 (Zephyr pinning), ADR 0013 (binary-blob policy, build
  profiles, and per-device Zephyr pinning — *not* the SDK-constraint
  grammar, which is PEP 440 per the amendment below), ADR 0015 §8
  (detached signing — the unsigned-image boundary), ADR 0017, ADR 0019;
  dashboard ADR 0007 (wire content).

## Amendment: request and result, the signing key in the context, and the explicit freeze (2026-08-09, product owner)

The session protocol of ADR 0019 gained an explicit freeze verb, and
that freeze splits this ADR's single manifest into two documents. Three
things ride along: the MCUboot public key becomes context content,
`verify` is defined against the effective context rather than a base
header, and a defect in this repository's own verification primitive is
recorded together with the duty that closes it.

Vocabulary, so decision 1 stays readable: what it calls "a builder" is
the **build container**, and what it calls "the lib" is the package set
of ADR 0020. The normative contract document is
[`build-container-contract.md`](../design/build-container-contract.md).

**`context.yaml` is the request; `manifest.yaml` is the result.**
Decision 6 describes one file that is two things at once: the pinning
*request* — what to build, with the constraints already resolved to
exact pins — and the integrity *record*, what the context turned out to
contain and what its ID is. The two cannot stay one file once a context
is uploaded in pieces: the pins have to exist before there is anything
to hash, and the `files` list cannot be complete until the client stops
adding to it.

- **`context.yaml`** travels in the base context. It carries the
  `context` format version, the resolved pins — container digest, SDK
  package sha256, target board — and the original intent (the
  constraint) that decision 3 requires to be recorded alongside the
  resolution. It is what carries the pins into a session, and policy is
  checked against it when it arrives (ADR 0019's amendment of the same
  date). `extend-context` MUST NOT touch it; an attempt is a typed
  error.
- **`manifest.yaml`** is written by `lock-context`, next to it: the
  same pins, plus the `files` integrity list and the context `id`. It
  is the *result* of freezing, never an input to it.

The undefined term "manifest header" in ADR 0019 §2 is retired with
this split — there is no header separate from `context.yaml`.

**Where each of decision 6's fields lives.** `context.yaml` carries all
of them except the two that cannot exist before the freeze (`files` and
`id`) — the three hashed pins, and with them every never-hashed field:
`created`,
`mcuhome.constraint`, `mcuhome.version`, `package.url`,
`container.image` and `container.tag`. That is where they belong,
because the request is the document in which decision 3 requires intent
and resolution to be recorded side by side, and because a value that
never enters an identity is only useful where a human reads back what
was asked for. `manifest.yaml` repeats the pin blocks — `mcuhome`,
`container`, `target` — exactly as `context.yaml` states them, and adds
`files` and `id`. It repeats rather than refers, so that the lock result
is readable on its own: the document that carries an identity carries
the inputs that identity was computed from. The one field that does not
travel is `created` — it dates the request, and the manifest's own
moment is the lock. The exact shape of both documents is normative in
[build-container-contract.md](../design/build-container-contract.md)
§3.2.

**Neither document is in the `files` list, and for `context.yaml` that
is a statement about the hash rather than about layout.**
`manifest.yaml` is excluded structurally: it is the document that
carries the list, so it cannot be an entry in it — the implementation
refuses one and skips the file when collecting entries
(`mcuhome/model/context.py:333-341`, `mcuhome/workbench/contextdir.py:116`).
`context.yaml` is excluded for a stronger reason. Hashing it as an
ordinary content file would readmit
`created` and `mcuhome.constraint` through the back door, and decision 6
excludes both from the identity **by name**. Two byte-identical device
configurations created a second apart would then hash differently, and
one configuration resolved once under `^2.3.6` and once under an exact
`2.4.0` would carry two identities for one resolved pin — the two
outcomes decision 6's exclusion list exists to prevent. Nothing is lost
by keeping it out: everything build-relevant `context.yaml` carries is
already hashed in its own right, because `container.digest`,
`sdk.sha256` and `target.board` are three of the four inputs of the rule
and the fourth is the `files` list itself.

The normative hashing rule of decision 6 is untouched. The hashed
structure is still `{container.digest, sdk.sha256, target.board,
files[]}` in RFC 8785 canonical JSON; only *when* and *from what* the ID
is derived becomes unambiguous. No build-relevant field is added, so
decision 6's extension rule is not triggered and no `context` format
version bump is taken.

**`keys/signing.pub` becomes context content — and that is a decision
about identity, not only about layout.** The context tree of decision 1
gains a `keys/` directory holding the MCUboot public key. It enters the
ID through the ordinary `files` list, on exactly the terms decision 2
sets for patches: no separate manifest section, no declared list that
could disagree with the bytes. The write whitelist of ADR 0019 §8 and
the contract's safe-extraction whitelist gain `keys/`.

It belongs in the hash because the public key is compiled into MCUboot
(ADR 0015 §8, "the public key is compiled into MCUboot" — which is why
rotation is a bootloader replacement there). Two builds with different
keys therefore produce different bootloaders, and two different
bootloaders must not share an identity.

The consequence has to be stated, because it is the decision and not a
side effect: **two users with byte-identical device configurations now
hash to two different context IDs.** Attribution stops encoding only
*what* was built and starts encoding *who* built it. That is correct —
the images genuinely differ — but it removes cross-user sharing from
the content-addressed identity of decision 4 by construction, so the
artifact cache that decision parked would be per-key if it were ever
built. In the corpus `signing.pub` exists today only as a filename
constant (`mcuhome/workbench/signing.py:81`) and a CLI-level parameter; this
makes it context content.

**The context is frozen by an explicit verb, and extension is bounded
to the phase before it.** ADR 0019's `lock-context` (see its amendment
of the same date) writes `manifest.yaml`, computes the context ID and
returns it. What that does to this ADR:

- Decision 5's "the *effective* context = base + extensions, re-hashed
  at build time" becomes **hashed once, at `lock-context`**. There is
  one moment at which the ID exists, and both sides compare their
  independently computed values there (contract §3.3).
- Decision 5's example — "patches created after a `verify`" — is no
  longer reachable inside one session: `verify` runs only after the
  lock, and the lock is one-way (`context.locked`). Adding a patch
  after a verify is a new session. Archivability is unchanged; the
  workbench still mirrors every piece it created.
- ADR 0019 §2's "`manifest.yaml` itself is **immutable for the
  session's lifetime**" is replaced rather than kept: before the lock
  there is no `manifest.yaml`. What is immutable for the session is
  `context.yaml`; the manifest is written once and is immutable
  afterwards by construction.

**`verify` checks the effective context.** Stated normatively because
the previous arrangement failed by construction: with a manifest that
was immutable for the session's lifetime *and* mid-session extension
allowed, `verify_context` reports "present but not in the integrity
list" for every file added after upload (`mcuhome/workbench/contextdir.py:349-351`)
and `ok` is therefore `False` (`:374-376`) after any extension at all.
Under the split above the contradiction disappears by construction: the
manifest `lock-context` writes *is* the integrity list of the effective
context, so verifying against it and verifying against the effective
context are the same act.

**A defect in the verification primitive, and the backend duty that
closes it.** `verify_context` (`mcuhome/workbench/contextdir.py:389-425`)
carries the docstring "the manifest's values are advisory, the bytes
decide" (`:392-393`), and that is true of exactly one of the four
hashed inputs. `container.digest`, `sdk.sha256` and `target.board` are
read straight out of the declared manifest when the actual ID is
recomputed (`:418-422`), and the manifest is itself excluded from the
integrity list by construction — deliberately, so it cannot influence
its own ID (`mcuhome/model/context.py:333-341`, `mcuhome/workbench/contextdir.py:116`).
The consequence nobody had written down:
**a self-consistently forged manifest passes with `ok == True`.** The
file hashes match, the recomputed ID matches, and all three declared
pins were simply believed.

It is not exploitable while the backend cross-checks those pins against
what it actually obtained — but nothing stated that duty, and the
docstring read as though the check were already complete. Both halves
are fixed:

- **The duty, normative:** the backend MUST verify `container.digest`
  against the image it pulled, `sdk.sha256` against the package bytes
  it fetched and unpacked, and `target.board` against the pins the
  session was admitted on. `verify_context` does not establish them.
  Recorded in ADR 0019 §8, where the "never trust client-declared
  hashes" floor lives.
- **The function** is corrected so that it cannot be mistaken for a
  complete check — what it measures is the `files` list, and it says
  so.

## Amendment: the SDK-constraint grammar is PEP 440 (2026-08-10, product owner)

Decision 3 says the device config carries constraints and the workbench
resolves them at context creation, but it never fixed the constraint
*grammar*. This resolves that, and corrects a misattribution that rode
along with the gap.

**The grammar is PEP 440 (decision E52).** An SDK constraint is a
[PEP 440](https://peps.python.org/pep-0440/) version specifier, resolved
with `packaging.specifiers.SpecifierSet` — `packaging` is already a
dependency, and PEP 440 is the version grammar the Python ecosystem
already agrees on, so a caret/tilde dialect of MCUHome's own would be one
more thing to specify, implement and get wrong. A constraint resolves to
the single **highest** available version that satisfies it, against a
**local** set of versions (for the SDK, the keys of the `index.json` a
source directory carries — `scripts/build_sdk_archive.py`); nothing is
fetched to resolve. The reference implementation is
`mcuhome/workbench/resolve_pins.py`.

**Pre-release rule.** A dev or pre-release version (`2.5.0.dev0`,
`2.5.0a1`) satisfies a constraint only when the constraint is itself a
pre-release specifier (`==2.5.0.dev0`, `>=2.5.0a1`) or pre-releases are
explicitly allowed — `SpecifierSet`'s own `prereleases` semantics. A
stable constraint such as `~=2.3` never resolves to a pre-release.

**The misattribution.** Earlier prose here (this ADR's Context, and the
`SdkPin` docstring) spelled constraints as `^2.3.6`/`~2.3.6` and traced
them to "ADR 0013's per-device pinning". Two things are wrong with that.
The caret/tilde spelling is npm-style, not PEP 440 — `~=2.3.6`,
`>=2.3.6,<3` and `==2.3.6` are the PEP 440 spellings of the same intents.
And ADR 0013 is binary-blob policy, build profiles and per-device
**Zephyr** pinning (`zephyr_version`, `blob_usage`); it never governed
the SDK-version-constraint grammar. The two were distinct decisions that
one sentence merged. The `^2.3.6` examples that remain in decision 6 and
in `build-container-contract.md` §3.2 are illustrative and pre-date E52;
read them as `~=2.3.6`. No normative rule changes: the constraint is
recorded in `context.yaml` as intent and is never hashed (§6), so the
grammar it is written in cannot affect a context ID.
