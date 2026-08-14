# MCUHome Build Container Contract — v1

> **Status: normative, approved by the product owner (2026-08-09).**
> This document specifies contract version 1 between an MCUHome build
> backend and a build container. Any container satisfying it is a usable
> build container — including third-party containers shipping their own
> toolchains. Rationale and the surrounding protocol are recorded in
> ADR 0018 (build context) and ADR 0019 (session protocol); this
> document stands alone and is intended to become the public
> "bring your own build container" specification.

The key words MUST, MUST NOT, SHOULD, SHOULD NOT and MAY are to be
interpreted as described in RFC 2119.

## 1. Terms and backend profiles

### 1.1 Terms

- **Build container**: a container image providing a build environment
  (toolchain, Zephyr, and any other source trees it builds against)
  plus **the program** (§2.2).
- **The program**: the executable a backend invokes to do one unit of
  work. In the container profile it lives at the fixed absolute path
  `/mcuhome/run` (§2.2).
- **Backend**: the software driving the build — either the workbench
  package driving a container runtime directly, or a build server. The
  backend owns everything outside the program: paths, trees, views,
  network isolation, resource limits, artifact egress.
- **Backend profile**: which of the two shapes of §1.2 serves a
  session. Declared in the `open-session` response (ADR 0019 §2).
- **Session**: the lifetime of one build environment. One session is
  bound to one build context; invocations within a session share state.
- **Build context** ("the context"): the self-contained input directory
  described in §3.
- **Invocation**: one execution of the program inside a session.
- **Action**: what the invocation is asked to do; the first operand of
  the invocation (§5.1). Contract v1 defines `describe`, `build` and
  `verify` (§7).
- **Working action**: an action that touches the context. In contract
  v1 those are `verify` and `build`; `describe` is the only action that
  is not one, and it is the only one that gets by on the request
  document's preamble alone (§5.2, §7.1). The term is used wherever an
  obligation applies to real work and not to a capability query.
- **Layer**: a source tree the program builds against, addressable by
  name for patching (§6). Contract v1 defines the layer names
  `zephyr`, `sdk`, `chip` and `mcuboot`. `mcuboot` is a layer because
  every device build is `west build --sysbuild` with MCUboot as the
  second image (`west.yml:47`) and because MCUboot is where the
  precedent for a patch of this kind was set (ADR 0015's RTT
  amendment: a compiled-out MCUboot log line). Layer names are an
  append-only registry owned by the MCUHome project; third-party layer
  names MUST carry an `x-` prefix, so that two vendors cannot collide
  on one name and have a context silently patch the wrong tree. `x-` is
  the whole third-party namespace: a layer name is a field name, and the
  grammar of §5.2 admits no dots.

### 1.2 The two backend profiles

| Profile | What it is | Guarantees |
|---|---|---|
| `container` | The backend materializes one container per session and invokes the program inside it. | Every isolation guarantee of ADR 0019 §8 applies: one session = one container instance = the trust boundary, no network, per-session resource limits and disk quota. |
| `subprocess` | The build environment runs **in the same filesystem as the build server**, but as a **separate process** (the Home Assistant App case): the backend runs the program as a subprocess in its own filesystem namespace instead of via `docker exec`. | Reduced, and named here so nobody assumes otherwise: **no network isolation, no per-session resource limits, no container trust boundary.** Cancellability and process-level isolation remain (the program is a separate process), and a third party may still implement the program in any language. |

What is shared in the `subprocess` profile is the **filesystem**, not
the process. A build server is an orchestrator in both profiles and is
never itself the build environment: it materializes paths, enforces
what it can enforce, invokes the program and reads its result. The
difference between the profiles is only how much of the environment the
kernel keeps apart — a container namespace in one case, a process
boundary inside one shared filesystem in the other.

The ABI is identical in both profiles: same invocation (§5.1), same
request document, same result document, same exit codes. The second
profile is a subprocess and not a library call on purpose: a build
running inside the server process cannot be cancelled without killing
the server, an out-of-memory kill or a segfault takes the queue with
it, and only a separate process is honest about the interface — the
build server's own code stated all three (build-server repo,
`mcuhome_buildserver/builder.py:5-28`, read at `8b8ceb4`; the file has
since been removed with the job protocol, and the build server imports
nothing from this package today). The argument outlived the file: it is
the reason this profile is a subprocess rather than the in-process
embedding it superficially resembles.

Because the filesystem is shared, nothing in this profile may depend on
a fixed path: several concurrent sessions live side by side in one
namespace and cannot all have the same context, work or output
directory. That is the reason contract v1 defines no mount points at
all (§4).

A `subprocess`-profile backend serves **exactly one build environment —
the one it runs in**. It MUST reject, typed, any session whose context
requires a Zephyr line that build environment does not carry.

Everything else in this document applies to both profiles unless a
paragraph says otherwise.

## 2. Build-container image requirements

These requirements apply to the `container` profile. In the
`subprocess` profile there is no image; the backend knows its own
program's path and its own identity, and answers the same questions
from `describe` (§7.1).

### 2.1 Labels

The image MUST carry these labels:

- `org.mcuhome.contract=1` — the contract version this document
  specifies.
- `org.mcuhome.zephyr=<version>` — the Zephyr version the image
  builds against.
- `org.mcuhome.toolchain=<id>` — the toolchain identity.

There is no label for the action set: every conforming program
implements `describe`, `verify` and `build` (§7), so there is nothing to
pre-filter on, and an action beyond those three is announced by
`program.actions` alone (§7.1.1).

The labels are **pre-start scheduling data**: they let a backend pick
an image before paying for a container start, and they carry the
compatibility constraint. They are **not** authoritative about what the
program can do — `describe` (§7.1) is, and a backend MUST check the
labels against `describe` before relying on them. Compatibility between
an SDK release and a container is declared as a constraint over the
coupling labels (`org.mcuhome.zephyr`, `org.mcuhome.toolchain`), never
as an enumeration of image tags: a tag or tag suffix carries no
compatibility meaning (ADR 0018 §7). Third-party containers qualify by
satisfying the same label constraint. §2.1.1 fixes what those two label
values may be and what a constraint over them looks like.

#### 2.1.1 The coupling labels: value range and constraint syntax

`org.mcuhome.zephyr` and `org.mcuhome.toolchain` are the two **coupling
labels**. They are the only labels a compatibility constraint may be
written over, because they are the two properties of an image that an
SDK release is actually coupled to.

**Value range.** Both values are single-line, non-empty, and drawn from
`[A-Za-z0-9][A-Za-z0-9._+-]*`. Beyond that each has a shape:

- `org.mcuhome.zephyr` — the upstream Zephyr version the image builds
  against, **without** the leading `v` west uses: `4.4.0` for the
  `v4.4.0` this project pins (`west.yml:32`). The value is a dotted
  numeric version, optionally followed by an upstream suffix after a
  hyphen (`4.5.0-rc1`).
- `org.mcuhome.toolchain` — the toolchain identity, as
  `<identity>-<version>`: an identity part matching
  `[a-z][a-z0-9]*(-[a-z0-9]+)*` and a dotted numeric version part after
  the final hyphen. MCUHome's own image builds with the Zephyr SDK, so
  its value is `zephyr-sdk-<version>` — `zephyr-sdk-1.0.1` for the
  version the image pins (`containers/build-container/Dockerfile:69`). A
  third-party image using a vendor toolchain names it in the same shape.

The identity part is **opaque and compared only for equality**; it is
never parsed, ordered, or mapped onto another name. Two toolchains with
different identity parts are simply different toolchains, whatever their
versions say.

**Constraint syntax.** An SDK release declares its compatibility as a
**map from coupling-label name to one constraint expression**, in the
SDK package's own metadata. The field name that carries the map is **not
fixed by this contract**, because the map is read by the backend when it
picks an image and never crosses into the container — unlike the three
names §6.1 freezes, which a build container itself must read. What is
fixed here is the map's shape. A constraint expression is one of:

| Form | Holds when |
|---|---|
| `=<value>` | the label value is byte-identical to `<value>` |
| `^<version>` | identity parts are equal **and** the label's version is ≥ `<version>` and below the next value of its leading non-zero component |
| `~<version>` | identity parts are equal **and** the label's version is ≥ `<version>` and below the next minor version |
| `>=<version>`, `>`, `<=`, `<` | identity parts are equal **and** the label's version compares accordingly |
| `>=<version> <<version>` | both bounds hold — the only compound form, a space-separated conjunction of exactly two comparisons |

`org.mcuhome.zephyr` has no identity part, so for that label the
identity condition is vacuously satisfied and only the version
comparison applies; for `org.mcuhome.toolchain` the identity part must
match before any version is compared at all.

Versions compare component-wise and numerically, with a missing
component read as zero, so `4.4` and `4.4.0` are the same version. A
value carrying a non-numeric suffix (`4.5.0-rc1`) is **not ordered at
all**: it satisfies only `=`, and it never satisfies a range. That is
deliberate rather than restrictive — an ordering over pre-release
suffixes is a well-known source of two implementations disagreeing, and
an SDK release that wants to bless a release candidate can name it
exactly.

Evaluation, so that no case is left to taste:

- **All named labels must hold.** The map is a conjunction; there is no
  alternation and no precedence to get wrong.
- **A label the constraint does not name is unconstrained.** An SDK
  release coupled only to Zephyr names only `org.mcuhome.zephyr`.
- **A container that does not carry a named label does not qualify.**
  Absence is never read as "compatible" — an image that does not say
  what it builds against has not made the declaration the constraint is
  written against.
- **The constraint is evaluated against the labels, never against the
  tag.** This is ADR 0018 §7's point restated where it is enforced: the
  `-rN` tag suffix is a build serial with no compatibility meaning, so a
  CVE respin that changes `r1` to `r2` and nothing else still satisfies
  every constraint that its predecessor satisfied, with no SDK release
  republished. A constraint that enumerated tags would have to be
  reissued for each respin, and a third-party image could never qualify
  at all — which is exactly why an enumeration is forbidden rather than
  merely discouraged.
- The labels remain pre-start data: a backend that has passed the
  constraint MUST still check the labels against `describe` (§7.1) —
  the constraint decides *which image to start*, not *what the program
  can do*.

### 2.2 The program

The image MUST provide an executable at the fixed absolute path
`/mcuhome/run` (script or binary) implementing the invocation ABI of
§5. It MUST be executable by **every** user the backend may exec as —
the backend runs the program as the calling user where it can
(`mcuhome/compiler/container.py:424-430`).

The path is absolute and fixed, and the program is **not** looked up on
`PATH`. Three reasons, all of which are properties of a filesystem this
contract does not control:

- `PATH` inside the image is the image author's to set; a bare name
  would be a promise about someone else's environment.
- `docker exec` inherits the environment fixed at container creation
  time, so `PATH` is not something the backend can correct at
  invocation time.
- The invocation is resolved without a shell, so there is no shell
  lookup to fall back on.

`/mcuhome/` carries the project name and reserves a namespace inside
the image, so an image author knows which directory is ours. The
filename says what the file *is*, not what it does: the action is an
operand (§5.1), so it cannot also be the name. There is no extension —
a third party may ship a compiled binary, and `.sh` would then be a
lie. The shape follows established practice for images implementing a
third-party contract: Cloud Native Buildpacks fix `/cnb/lifecycle/*`
with `bin/detect` / `bin/build`, and Dev Container Features fix
`install.sh`; neither brands the executable with the vendor's name.

**Starting the container is the backend's business, not the image's.**
The command that runs as the container's main process is the backend's
to choose — `docker run` overrides both `ENTRYPOINT` and `CMD` — and
keeping the container running for the session is the backend's job. A
conforming image therefore MUST NOT depend on its own `ENTRYPOINT` or
`CMD` being used, and MUST tolerate being started with a command the
backend names instead. So that there is always such a command to name,
the image MUST provide a POSIX shell at `/bin/sh`; it is the cheapest
thing an image can carry that lets a backend start it at all. Which
command the backend names, and how it keeps the container running
between invocations, is not part of this contract.

Apart from the labels of §2.1, `/mcuhome/run` and a POSIX shell at
`/bin/sh`, this contract makes no demand on the image's contents.

In the `subprocess` profile the backend invokes its own program by a
path it configures. The argv shape, the request document, the result
document and the exit codes are identical; only the path is the
backend's business.

#### 2.2.1 `/mcuhome/describe.json` — the optional static self-description

An image MAY carry a file at the fixed absolute path
`/mcuhome/describe.json`. It is **optional** in the strict sense: an
image without it is fully conforming, and nothing else in this contract
changes for either party.

Its content is a `describe` result document (§5.4, §7.1.1) — exactly
what invoking `describe` on this image answers, and nothing else. The
strongest way to keep that true is to not write the answer twice:
MCUHome's own image generates the file at image build time by *running*
`describe` and storing the result document unread, so the file cannot
state anything the program would not. An image author who assembles it
by hand takes on the duty of keeping two answers equal, and that duty is
the whole cost of the file.

**What a backend does with it.** Where the file is present and parses as
a result document, a backend MAY read it **instead of invoking
`describe` before the container is arranged**. Where it is absent,
unreadable, or where the backend would rather ask, the backend invokes
`describe` exactly as it does today. There is no new failure mode in
either direction, because the fallback is the thing that was already
mandatory.

`describe` remains **authoritative** (§7.1). This file is pre-start data,
like the labels of §2.1, and it is bound by the same rule: a backend MUST
NOT rely on a static answer that a `describe` contradicts, and an image
whose file disagrees with its own program is in violation of this section
exactly as an image whose label disagrees with `describe` is in violation
of §2.1.

**Why an image would carry it at all.** §6.1 permits a program whose
*body* arrives with a mounted tree, and MCUHome's own image is the worked
example: the launcher at `/mcuhome/run` is image content, the body
arrives with `trees.sdk`, and the launcher cannot run without it. For
such an image the `program` block is unobtainable until the backend has
decided where to mount that tree — while `trees`, inside that same block,
is precisely what tells the backend where the mount has to go. The image
is undiscoverable before the mount point is known, and the mount point is
what discovery would have supplied. That circle is what the static file
cuts, and it cuts it without weakening §7.1: the image answers a question
about itself with the answer its own program computed.

The file has no meaning in the `subprocess` profile, where there is no
image to carry it and `describe` is the only discovery channel there is.

## 3. The build context

### 3.1 Layout

```
context/
  context.yaml             # the request: format version and resolved pins
  manifest.yaml            # the lock result: pins + integrity list + context ID
  model/device-model.json  # canonical device model
  keys/signing.pub         # MCUboot verification key (public half only)
                           # required for `build`, not for `verify`/`describe`
  patches/                 # optional
    zephyr/0001-*.patch    # layer = subfolder, order = filename prefix
    sdk/0001-*.patch
    chip/0001-*.patch
    mcuboot/0001-*.patch
```

- `manifest.yaml` is the program's entry point; a program MUST NOT
  require any out-of-band knowledge beyond it and this contract.
- A patch's target layer is its subfolder; its application order is its
  filename. There is no patch list in the manifest — the patch set of a
  layer is defined by the files present under `patches/<layer>/`,
  applied in ascending lexicographic filename order (the `NNNN-` prefix
  convention; ADR 0018 decision 2).
- `keys/signing.pub` is the **public** half of the user's MCUboot
  signing key (`mcuhome/workbench/signing.py:81`). It is context content and an
  ordinary entry of the `files` list, which is correct: MCUboot
  verifies against a key compiled into the bootloader, so two builds
  with different keys produce different bootloaders and must not share
  an identity. The private half never reaches a build container
  (ADR 0015 decision 8). It is **required for `build`** and not required
  for `verify` or `describe` (§7.2).
- Build outputs MUST NOT be written into the context. The context is a
  read-only input for the whole life of a session (§4).
- There is no `.mcuhome/` directory. In contract v1 as first drafted the
  backend wrote its per-invocation document there; it now lives outside
  the context entirely (§5.2), which is what makes the context a
  genuinely read-only mount.

### 3.2 The two context documents

`context.yaml` is written when the base context is created. It carries
the format version, the resolved pins, the build environment the context
requires, and the original intent — and nothing that depends on the
final file set:

```yaml
context: 2                          # context format version
created: 2026-08-09T10:00:00Z       # informational — never hashed
mcuhome:
  constraint: ~=2.3.6               # original intent — never hashed
  version: 2.4.0                    # resolved exact pin
  package:                          # resolved SDK package
    url: https://…/mcuhome-sdk-2.4.0.tar.zst   # hint only — never hashed
    sha256: <hash>
zephyr: '4.4'                       # REQUIRED Zephyr line — never hashed
target:
  board: nrf7002dk/nrf5340/cpuapp
```

Both never-hashed fields of the pin are required **keys** whose value
MAY be the empty string. An empty `constraint` is PEP 440's own
any-version specifier — no intent was stated. An empty `package.url`
means the package was resolved from a location with no public name; a
context resolved from a local directory records no `file://` URI,
because that would carry the creator's filesystem layout into a
document another party stores. A reader MUST treat a *missing* key or
a non-string as malformed, and an empty statement as a statement.

**A context requires a build environment; it does not choose one.**
`zephyr` names a Zephyr release *line* — dotted decimal numbers, no
leading `v`, no pre-release suffix — and a build container serves it
when its `org.mcuhome.zephyr` label (§2.1.1) is a release *within* that
line: `4.4` is served by `4.4`, `4.4.0` and `4.4.12`, and by nothing
else. A release carrying an upstream suffix (`4.5.0-rc1`) serves no
line, for the reason §2.1.1 gives about ranges. A container that carries
no `org.mcuhome.zephyr` label at all serves no line either — absence is
never read as compatible.

Selecting a container of that line is the **backend's** job, and the
selection MUST be recorded in `manifest.yaml` (below). A backend that
serves no container of the required line MUST refuse, typed, before the
context is locked. The `zephyr` value is written quoted, because YAML
reads an unquoted `4.4` as a number.

The value is **informational for the hash** and load-bearing for
everything else: §3.3 excludes it, and §3.3's own note says why that is
redundancy rather than a hole.

A context may be extended after creation (ADR 0019 §2,
`extend-context`), and an extension MUST NOT touch `context.yaml` — it
carries the pins the session was admitted on, and changing them is a
new session, not an extension.

`manifest.yaml` is written **by the backend when the context is
locked** (`lock-context`, ADR 0019 §2). It repeats the same pins and the
same requirement, adds the backend's answer to that requirement, and
adds the two things that only exist once the file set is final:

```yaml
context: 2
mcuhome: { … }                      # as in context.yaml
zephyr: '4.4'                       # as in context.yaml — the requirement
container:                          # THE BACKEND'S RESOLUTION — never hashed
  image: ghcr.io/mcu-home/build-container
  tag: zephyr-4.4.0-r1
  digest: sha256:<hash>             # null for an image that was never pushed
target: { … }                       # as in context.yaml
files:                              # integrity list: every content file,
  - { path: keys/signing.pub, sha256: <hash> }             # patches included
  - { path: model/device-model.json, sha256: <hash> }
  - { path: patches/zephyr/0001-fix.patch, sha256: <hash> }
id: sha256:<hash>                   # canonical hash (identity), rule in §3.3
```

The `container` block is **written by the party that locks the context
and by nobody else**. It is the record of which build environment
answered this context's requirement, and it is what reproduces the build
years later: the requirement says what was needed, this says what
actually ran. It is outside the identity (§3.3), so two backends
answering one requirement with two different containers of the same line
still compute one context ID for one set of bytes. `digest` is the repo
digest, or `null` for an image that was built locally and never pushed —
such an image names no fetchable bytes, and `null` says exactly that
rather than inventing a value.

Every `<hash>` in the two documents above has exactly one legal
spelling, fixed normatively in §3.3.1: `container.digest` (where it is
not `null`) and `id` are `sha256:` followed by 64 lowercase hex digits,
`mcuhome.package.sha256` and every `files[].sha256` are 64 lowercase
hex digits with no prefix. Any other rendering is invalid input and is
refused, never normalized — the ID is a hash over a text encoding, so
the spelling *is* part of the identity.

A program MUST check the `context` format version and, for a version it
does not implement, fail the invocation with `status: "unsupported"`,
`reason: "unsupported.context"` (§5.4) and the version it found in
`error.details`. It is `unsupported` and not `failure` for the same
reason an unknown `request` version is: the program is refusing a
document written to a specification it does not have, which a backend
can act on by choosing another image — nothing about this context is
broken.

**A build container only ever sees a locked context.** The working
actions (`verify`, `build`) are unlocked by `lock-context` and nothing
may extend the context afterwards, so within a session `manifest.yaml`
is immutable and the program MAY assume it is identical across all
invocations of the session. This is a property of the session protocol,
not a rule the program has to enforce.

The `files` list covers every content file of the context. It does
**not** cover the two context documents themselves: `manifest.yaml` and
`context.yaml` are both excluded from the integrity list, and therefore
from the context ID.

For `manifest.yaml` the exclusion is structural — it is the document
that carries the list.

For `context.yaml` it is a consequence of ADR 0018 §6, which excludes
`created` and `mcuhome.constraint` from the hash by name. Hashing
`context.yaml` as an ordinary file would readmit both through the back
door: two byte-identical device configurations, created a second apart,
would hash differently, and the same configuration built once under the
constraint `~=2.3.6` and once under `2.4.0` would produce two identities
for one resolved pin. Nothing is lost by the exclusion, because
everything build-relevant `context.yaml` carries is already hashed in
its own right — `sdk.sha256` and `target.board` are two of the three
inputs of §3.3, the third is the `files` list itself, and `zephyr` is
covered by the argument §3.3 makes about it.

### 3.3 Context identity — normative hashing rule

This rule is **locked for context format version 2 and can never
change**. New build-relevant fields enter the hash only together with
a `context` format-version bump. `lock-context` changed *when* and
*from what* the ID is derived; it did not change the rule.

The context ID is the SHA-256 hash, rendered as `sha256:` followed by
exactly 64 lowercase hex digits (§3.3.1), of the RFC 8785 (JSON
Canonicalization Scheme) encoding of exactly this JSON structure:

```json
{
  "files": [{"path": "<path>", "sha256": "<hash>"}, …],
  "sdk": {"sha256": "<hash>"},
  "target": {"board": "<board>"}
}
```

- `sdk.sha256` — the manifest's `mcuhome.package.sha256`.
- `target.board` — the manifest's `target.board`.
- `files` — one entry per context file, `path` and `sha256` as in the
  manifest's `files` list, sorted by `path` in ascending byte order **of
  its UTF-8 encoding**, which is the same as ascending code-point order
  and is *not* the UTF-16 code-unit order RFC 8785 uses for object keys —
  the two differ as soon as a path outside the BMP meets one inside it.
  Duplicate paths are invalid. Patches and `keys/signing.pub` are
  ordinary entries; every listed file contributes its own content hash,
  the sort only makes the encoding deterministic.

Explicitly excluded from the hash: `created`, `mcuhome.constraint`,
`mcuhome.package.url` (any source yielding the pinned hash is
equivalent), the whole `container` block, and `zephyr`.

The **`container` block** is excluded because it is not the context's
statement. It is the backend's record of which build environment
answered the context's requirement (§3.2), and a context that identified
itself by it would get a different identity on every server that serves
the same Zephyr line with a different image — which would make "built
from *this* context" a claim about a machine rather than about a set of
bytes.

**`zephyr`** is excluded because hashing it would be redundancy rather
than identity. The line is provably inside two values the hash already
covers: it is a property of the SDK release that `sdk.sha256` pins, and
it is a field of the canonical device model, whose bytes are an ordinary
entry of `files`. Two contexts differing only in their required line
already have two IDs. A third copy of the fact would enlarge the hashed
document without narrowing what it identifies — and every field in that
document is one more thing two implementations have to agree about.

That argument holds only while the two copies agree, and `context.yaml`
is outside the integrity list by construction, so nothing about the
*bytes* enforces it. §9.1 therefore makes the comparison a backend duty:
a context whose `zephyr` differs from its model's `toolchain.zephyr_line`
is refused. Without it the exclusion is circular — two contexts with
identical files and different required lines would share one ID and
build in containers of two different lines.

The hash input is **never** the YAML file bytes and **never** the
transport archive bytes: neither serialization is deterministic.

The **effective context** of an invocation is the context as
materialized at invocation time; its ID is computed by the same rule
over the files then present. Implementations on both sides of the
contract MUST compute the ID independently from the bytes they actually
hold and MUST NOT trust a declared `id` value.

Recomputing over the `files` list is **not** a complete check of the
context, and no implementation may present it as one: two of the three
hashed inputs — `sdk.sha256` and `target.board` — are read from
`manifest.yaml`, which is not itself in the integrity list, so a
self-consistently forged manifest recomputes to its own declared ID.
The reference implementation has exactly this shape
(`mcuhome/workbench/contextdir.py:666-701`, pins taken from the declared
manifest at `:695-697`, only `files` measured). Closing it is a backend
duty and it is stated as one in §9.1.

#### 3.3.1 The lexical form of a hash value — normative

The ID is a hash over a text encoding, so the *spelling* of every hash
that goes into it is part of the rule. Contract v1 fixes exactly one
spelling per value. There is no second accepted rendering and no
normalization step anywhere on either side of the contract:

| Value | Form |
|---|---|
| `container.digest` (in `manifest.yaml`; the one hash value that is outside the hashed structure, and `null` where the image was never pushed) | the literal prefix `sha256:` followed by exactly 64 lowercase hex digits — `[0-9a-f]{64}` |
| the context `id` (in `manifest.yaml`, and `result.context` in §5.4) | the same: `sha256:` + exactly 64 lowercase hex digits |
| `files[].sha256` (in `manifest.yaml` and in the hashed structure) | exactly 64 lowercase hex digits, **no prefix** |
| `mcuhome.package.sha256`, which the hashed structure carries as `sdk.sha256` | exactly 64 lowercase hex digits, **no prefix** |

Uppercase or mixed-case hex, a missing prefix where one is required, an
added prefix where none is, whitespace, a `0x` form, a truncated or
over-long digest: each of these is **invalid input, not something to
normalize**. An implementation that encounters one MUST refuse the
manifest, naming the offending value, and MUST NOT compute an ID from
it.

The reason is the purpose of the ID. Normalizing would give the same
bytes two names — a manifest with an uppercase digest and a manifest
with the lowercase one would be accepted as one context under one ID,
so the mapping from spelling to identity would stop being a function of
the bytes. One spelling per value is what makes it impossible for two
manifests to name the same bytes differently, and it is also what stops
a mistyped digest from being silently accepted: an ID computed over a
mistyped digest is wrong forever, because the ID is frozen.

This is not a new rule; it is the rule the reference implementation
already enforces, with two separate regular expressions and no
normalization path between them:
`_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")` and
`_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")`
(`mcuhome/model/context.py:172-173`), applied by `_require_sha256`
(`:473-483`) to every input of `context_id` before it hashes anything
(`:573-600`) and by `_require_digest` (`:485-491`) to the two values
that carry the prefix. The first states the reason in its own refusal —
"one spelling per hash, so two manifests can never name the same bytes
differently" (`:477-482`) — and
the function's docstring states the other half: inputs are "checked
strictly rather than normalized: an ID computed over a mistyped hash
would be silently wrong forever, and normalizing (say, uppercase hex)
would give the same bytes two names" (`:587-589`).

The rendering of a hash **outside** the hashed structure follows one
rule, so that no reader has to look a field up: the algorithm is named
exactly once per value, either in the key or in the value. A value under
a key that already names the algorithm — `artifacts[].hashes.sha256`
(§5.4) — is bare 64 lowercase hex digits. A value that carries its own
algorithm — `container.digest`, the context `id`, `result.context`,
`layers[<name>].patchset` (§5.4) — is `sha256:` + 64 lowercase hex
digits.

## 4. Paths, trees and the filesystem interface

**Contract v1 defines no mount points.** Every path the program needs
arrives in the request document (§5.2), as an absolute path chosen by
the backend. `/ctx`, `/out` and `/ccache` may be used as conventions by
an image or a backend, but a program that *depends* on one of them is
not conforming: `context`, `out`, `work`, `tmp`, `ccache`, `result`,
`events` and `cancel` are the backend's to place, and MCUHome's
conformance suite deliberately moves every one of them.

The reason is the `subprocess` profile: several concurrent sessions
live in one filesystem namespace and cannot all have `/ctx`. Fixed
paths are also wrong in the `container` profile, for the smaller reason
that a fixed path is a promise about a filesystem the contract does not
own.

**A `trees` entry is the one thing a program may have a fixed path
for**, and it may because a tree is a property of the *image* rather
than of the session. A program MAY declare, in `describe`'s
`program.trees` (§7.1.1), the path at which it requires a tree to be
provided; a declared path is then a requirement the backend MUST
satisfy for that image, and not a convention. The case that exists
today is the SDK: a program whose build environment is a west workspace
resolves project paths from `.west/config` and the manifest, and has no
way to re-point them at invocation time, so "put it where you like" is
a promise it cannot keep and would have to break in the middle of a
build. A `path` of `null` keeps its meaning exactly — the program
carries no tree there and requires nothing about where one goes.

What the paths above have and a tree does not is a per-session
identity: each of them belongs to one invocation or one session — the
shared `ccache` to one backend — which is why two sessions in one
namespace cannot share a spelling of them and why none of them is an
image's to name. A declared tree path is the same declaration for every
session the image serves, so nothing about the path is negotiated per
session — what the backend puts *at* it still is, and a backend that
cannot give each concurrent session its own view of that path cannot
use that image. It learns so from `describe`, before it starts a
session and before it promises a client anything: that is the whole
difference between a declared requirement and a path compiled into an
image.

What the backend provides per invocation:

| Request field | Mode | Contents |
|---|---|---|
| `context` | RO | the build context (§3), materialized and integrity-checked by the backend |
| `out` | RW, empty | the invocation's output directory; artifact paths are relative to it |
| `work` | RW | the session's persistent working area, exclusive to this session |
| `tmp` | RW, empty | per-invocation scratch; the program points its children's `TMPDIR` here |
| `trees.<layer>` | RO or RW | one source tree per layer the program is to build against (§4.1) |
| `ccache` | optional | shared compiler cache (§10) |
| `result`, `events`, `cancel` | RW files | protocol channels (§5.2, §8) |

`work` exists because "the container's own filesystem is the working
area" is true only in the `container` profile. In the `subprocess`
profile there is no container, and an unnamed working area resolves to
a path collision in which two sessions destroy each other's CMake tree
— silently, because a tree overwritten mid-build produces a confusing
compiler error rather than an obvious one.

### 4.1 Trees

`trees` maps a layer name (§1.1) to `{path, writable}`.

- The backend MUST supply a `trees` entry for **`sdk`** for every
  working action (§1.1). The SDK package is never part of the image:
  it is fetched and unpacked per session, hash-verified against
  `mcuhome.package.sha256` (§9.1).
- The backend MUST supply a `trees` entry, with `writable: true`, for
  **every layer that carries patches** (§6).
- For an unpatched tree that lives inside the image, the backend MAY
  omit the entry; the program then uses its own. The program MUST NOT
  require an entry it does not need for the requested action.
- `writable` is **asserted by the backend, never probed by the
  program.** A program cannot reliably distinguish a read-only bind
  mount from a permission problem or a full disk by trying to write,
  and in the `subprocess` profile a tree may be writable to the
  filesystem and forbidden by policy at the same time.

This resolves an internal contradiction of contract v1 as first
drafted, where §4 mounted `/sdk` read-only while §6 required a writable
view of the `sdk` layer at that same path. There is one statement now:
`trees.sdk.writable` is true exactly when the backend has provided a
writable view of the SDK tree, which it does exactly when the `sdk`
layer carries patches.

## 5. Invocation ABI — frozen in contract v1

### 5.1 The invocation

The backend executes each invocation as:

```
/mcuhome/run <action> <absolute path of the request document>
```

Exactly two positional operands, both mandatory, **never a flag**. Any
other arity, and an `argv[2]` that is not absolute, is exit 66 (§5.3,
§5.2 rule 5). A program needs `argv[1]`, `argv[2]` and a JSON parser.
Nothing else — no environment variable carries information the program
needs, and the working directory is meaningless: a program MUST NOT
rely on any `cwd`.

**This argv is frozen and never grows.** Extensibility runs through the
request document only. The reason is the purpose of the whole contract:
a user must be able to build and run a completely own build container
and still work inside MCUHome's system. A new argv parameter breaks
every third-party container that does not know it; an unknown JSON
field is ignored by an older program and costs nothing. That asymmetry
is the entire argument, and it is why the invocation is deliberately
this small.

The action lives in argv and **not** in the request document. Contract
v1 as first drafted put it in both and then had to mandate that they
agree, without defining what happens when they do not. Two sources of
one truth are a defect generator in a specification that can never be
changed.

**The bootstrap chain**, step by step. Every step resolves with the
information the previous one supplied; there is exactly one unresolved
link at the start, and it is resolved in one jump.

| # | Step | Why it closes |
|---|---|---|
| 1 | The backend creates a **backend-owned per-invocation directory** and writes the request document into it atomically. | It is not inside the context, so `context` can be a kernel-enforced read-only mount. It also removes the data race the fixed path `/ctx/.mcuhome/command.json` had, where two concurrent `docker exec` invocations overwrote each other's document. |
| 2 | The backend invokes `/mcuhome/run <action> <request-path>` — via `docker exec` in the `container` profile, as a subprocess in the `subprocess` profile. | One ABI, two profiles. |
| 3 | The program reads argv: exactly two operands, the second of them an absolute path. | Otherwise exit 66. |
| 4 | The program opens and parses the request document. | This is the **only** program-caused error that cannot produce a result document — and precisely the case in which the program does not know where a result would go. |
| 5 | The program reads `request` and `result` from the immortal preamble (§5.2). | From here on **every** error is a result document, including "I do not implement this request format version" — because the preamble guarantees `result` is a top-level string path in every future request format version. |
| 6 | The program takes the action from `argv[1]`. | One source, not two. |
| 7 | The program checks `required` against its explicit list of honoured JSON Pointers **and the values it can honour there** (§5.2). | An old program refuses legibly instead of quietly doing something else. |
| 8 | The program reads the remaining fields it needs. | Nothing is composed, derived or guessed from them. |
| 9 | The program assembles its build environment from `context`, `trees` and `work` (§6). | See §6 — four paths are not a build. |
| 10 | The program writes the result document atomically to `result` and exits 0 or 1. | The last write action of the invocation. |

### 5.2 The request document

UTF-8 without BOM, one JSON object, RFC 8259. Duplicate keys are
invalid. `null` never means "absent"; it is invalid. That rule governs
**this** document only — the result document uses `null` with meaning
and defines it per field (§5.4). Every path value is absolute. The
document lives in a backend-owned per-invocation directory and **never
inside the context**.

```jsonc
{
  // ---- immortal preamble: present in EVERY future request format version,
  //      at the top level, with these names and types ----------------------
  "request": 1,                                   // request format version
  "result":  "/srv/mh/s-42/inv-7/result.json",    // the exact file for the result

  // ---- identity (echo token; NEVER used to build a path) -----------------
  "session": "s-42",

  // ---- writable areas ----------------------------------------------------
  "out":  "/srv/mh/s-42/inv-7/out",   // empty, per invocation; IS the target directory
  "work": "/srv/mh/s-42/work",        // persistent across the session, exclusive
  "tmp":  "/srv/mh/s-42/inv-7/tmp",   // per invocation; the program points TMPDIR here

  // ---- read-only inputs --------------------------------------------------
  "context": "/srv/mh/s-42/ctx",      // the build context (§3)

  // ---- source trees (§4.1) -----------------------------------------------
  "trees": {
    "sdk":     {"path": "/srv/mh/s-42/sdk",         "writable": false},
    "zephyr":  {"path": "/srv/mh/s-42/view/zephyr", "writable": true},
    "chip":    {"path": "/opt/connectedhomeip",     "writable": false},
    "mcuboot": {"path": "/opt/bootloader/mcuboot",  "writable": false}
  },

  // ---- optional shared cache (§10) ---------------------------------------
  "ccache": {"path": "/srv/ccache", "writable": false},

  // ---- budgets -----------------------------------------------------------
  "limits": {
    "jobs": 4,                        // AUTHORITATIVE; mandatory for working actions
    "memory_bytes": 8589934592,       // advisory
    "deadline_seconds": 5400,         // relative, from program start
    "cancel_grace_seconds": 60
  },

  // ---- optional channels (§8) --------------------------------------------
  "events": "/srv/mh/s-42/inv-7/events.ndjson",   // absent ⇒ no events
  "cancel": "/srv/mh/s-42/inv-7/cancel",          // EXISTENCE means: stop

  // ---- action parameters -------------------------------------------------
  "params":   {"mode": "incremental"},            // absent ⇒ {}; no mode ⇒ "clean"
  "required": ["/params/mode", "/trees/zephyr"]   // absent ⇒ []
}
```

**Field-name grammar (frozen):** `[a-z][a-z0-9_-]*`, plus the `x-`
prefix for third parties. Hyphens are required because layer names
admit them (`mcuhome/workbench/contextdir.py:69`) and a layer named `some-layer`
must be addressable from `required`.

**Mandatory in v1:** `request` and `result` for **every** action.
Additionally for every working action: `session`, `out`, `work`, `tmp`,
`context`, `trees.sdk`, `limits.jobs`. `describe` needs
only the preamble. §5.4 states the mirror image of this list for the
result document, and the two are deliberately symmetric: a `describe`
that was handed nothing but the preamble is never expected to return
more than it was given (§5.4, the echo rule).

**Parsing rules — the complete list:**

1. Unknown fields at any level: **ignore them**.
2. Every entry of `required` is an RFC 6901 JSON Pointer into *this*
   document. The program keeps an **explicit list of the pointers it
   actually honours**, and knowing the path is not enough: **it must be
   able to honour the value it finds there.** Otherwise the invocation
   produces `status: "unsupported"`, `reason:
   "unsupported.required"`, and the offending pointers in
   `error.details.required`.
3. A field the program needs for this action and does not find, or a
   `request` version it does not implement ⇒ `status: "unsupported"`,
   `reason: "unsupported.request"`. This is always possible because
   `result` is in the immortal preamble.
4. A path value that is not absolute ⇒ `unsupported.request`. `result`
   is exempt: rule 5 governs it.
5. Only if the argv arity is wrong, `argv[2]` is not absolute, the
   document is missing, does not parse, is not an object, or `result`
   is missing, is not a string, is not absolute or is not writable ⇒
   **exit 66, nothing written**.

Rule 4 cannot reach `result` itself, which is why rule 5 does: a
program that found a relative `result` would have to write
`unsupported.request` *into* that path, and §5.1 leaves it nothing to
make the path absolute with — the working directory is meaningless, so
the only honest resolution is none at all. The same holds one step
earlier for `argv[2]`. A relative request path resolves against
whatever directory the backend happened to leave the process in, so it
finds nothing or, worse, finds a **different** request document and
answers that one with exit 0 — the single silent failure this ABI would
otherwise have, in which every party involved believes the invocation
succeeded. Both cases are therefore exit 66 with nothing written,
rather than a refusal nobody could read.

`session` is an **opaque token**. A program MUST NOT compose any path
from it, from the parent directory of `argv[2]`, or from a compiled-in
absolute path. Composing a path from a JSON string is an error class (an
empty ID writes into the parent directory, a `../` escapes) that simply
does not need to exist. `session` earns its place by letting the program
recognise a `work` directory left behind by a different session — §6.3
specifies what it does when it finds one.

There is **no invocation ID** in the request document. The backend
addresses an invocation by the `out`, `result` and `events` paths it
chose for it, so a token the program could only echo back would be one
more field for a third party to get right for nothing.

`limits.jobs` is **authoritative** and mandatory for working actions.
It is not a hint: MCUHome's own implementation needs three separate
channels to get a job count into a build because none of them inherits
(`mcuhome/compiler/workspace.py:165`, `:175`, `:185`) and resolves the number
host-side on purpose, because the container sees the host CPU count but
not the RAM budget (`mcuhome/compiler/workspace.py:359-394`, `:598`). In the
`subprocess` profile the program runs directly on a shared host, so
`nproc` reports the whole machine, and several concurrent sessions at
`nproc` jobs each is an out-of-memory kill. An optional field would be worthless here: a
foreign program would fall back to `nproc`, which is exactly the case
the field exists against.

`limits.memory_bytes` and `limits.deadline_seconds` are advisory —
enforcement is the backend's (§9.1). `deadline_seconds` is relative to
program start.

**`params` is optional, and its absence has a defined meaning.** A
backend MAY omit `params` entirely. An absent `params`, a `params`
object without a `mode` key, and `params: {}` are the same thing and all
three mean **`mode: "clean"`** (§7.2). A program MUST NOT treat a
missing `mode` as "whatever is cheapest", as "repeat the last build's
mode", or as an error.

The default is the safe one, not the fast one, and that is the reason it
is stated here rather than left to the program: `clean` is the mode that
never silently reuses state. A defaulted `incremental` would let a
backend that says nothing inherit a warm workspace it did not ask for,
which is exactly the case in which nobody is in a position to notice —
the artifact is produced, it is attributed to the context ID, and
whether a stale object file from an earlier invocation went into it is
invisible in the result. `clean` is also what §7.2 requires for release
artifacts, so the value a caller gets by saying nothing is the value a
caller gets by asking correctly.

**`required` and value granularity.** Because the request document is
the only extension channel, future fields are top-level fields, and an
old program would ignore a top-level field that changes the *meaning*
of the artifact and then report success. Two rules prevent that: the
comparison runs against an explicit list of honoured pointers, not
against "is the path present in the document"; and the *value* counts,
not only the name. A program that knows `/params/mode` but not the
value `reproducible` MUST refuse with `unsupported.required` rather
than accept the job and quietly deliver something else. This is the one
place where the extension mechanism can actually break, and it costs a
sentence.

### 5.3 Exit codes — frozen

| Code | Meaning | Result document |
|---|---|---|
| **0** | The invocation ran and the work succeeded. | present, `status: "success"` |
| **1** | The invocation ran and the work did not succeed. | present, `status` ∈ `failure` \| `unsupported` \| `cancelled` |
| **66** | The request was unusable; no result could be addressed. | absent |
| anything else | **The program died.** Undefined forever. | undefined |

Contract v1 as first drafted reserved 64 for "unsupported command" and
65 for "unsupported required parameter". Both are **removed**. They are
`EX_USAGE` and `EX_DATAERR` from BSD `sysexits.h`, which foreign
runtimes emit for ordinary argument errors — a Go program returning 64
on a typo would be read as "action not supported" and its work
rescheduled onto another image. And why an action was refused is an
enumerable, growing list, so it belongs in the result document, not in
a frozen number. Nothing is deployed against contract v1 yet, so
continuity costs nothing here.

**The backend rule that makes the exit codes meaningful:** the backend
reads the result document **if it exists**, regardless of the exit
code. An invocation is successful exactly when all of the following
hold — the document parses and names an implemented `result` version;
it carries every field §5.4 makes mandatory for the invoked action;
`action` echoes `argv[1]`, and every echo field the request document
actually supplied echoes correctly; `status == "success"`; the observed
exit code, where observed, is 0; every declared artifact exists as a
regular file under its declared `root` and re-hashes to its declared
value; and, for a working action, the backend's own context ID matches
`result.context`. Anything else is not a success. Where exit
code and document contradict each other, the pessimistic reading wins
**and** a contract violation is raised against the image.

### 5.4 The result document

Written to the exact path given in `result`, as the **last write
action** of the invocation, atomically (temporary file in the *same*
directory, `fsync`, `rename`). The file's permissions are the
backend's concern and not the contract's: the backend chose the path,
and it either runs the program as itself or outranks it, so it can
always read back what it asked for. The one thing a program MUST NOT
do is make the result document unreadable to its own uid and gid — an
answer that exists and cannot be collected is worse than no answer,
because the exit code then contradicts a document nobody can look at.

```jsonc
{
  // ---- immortal preamble: present in EVERY future result format version ----
  "result": 1,
  "status": "success",        // success | failure | unsupported | cancelled
                              // Consumers MUST treat unknown values as failure.

  "action":  "build",         // echo of argv[1] — always present
  "session": "s-42",          // echo — iff the request carried it

  "reason":  null,            // dotted, append-only, x-* for third parties; null on success
  "error":   null,            // on != success: {retryable, message, details} — §5.4.1

  "context": "sha256:…",      // effective context ID, computed by the program itself

  "program": {                // self-description — field by field in §7.1.1
    "id": "org.mcuhome.build-container", "version": "2.4.0",
    "contract": 1, "request": [1], "result": [1],
    "actions": ["describe", "verify", "build"],
    "trees": {"zephyr":  {"path": "/opt/zephyr",          "version": "4.4.0"},
              "chip":    {"path": "/opt/connectedhomeip", "version": "v1.5.1.0"},
              "mcuboot": {"path": "/opt/bootloader/mcuboot", "version": "…"},
              "sdk":     {"path": null}}   // null = "wherever you put trees.sdk"
  },

  "layers": {                 // what was ACTUALLY applied (§6)
    "zephyr": {"patchset": "sha256:…"}
  },

  "artifacts": [
    {"root": "out", "path": "firmware.hex", "role": "firmware",
     "hashes": {"sha256": "…"}}
  ]
}
```

**Mandatory in v1, per action:**

| Field | `describe` | `verify` | `build` |
|---|---|---|---|
| `result`, `status` | MUST | MUST | MUST |
| `action` | MUST | MUST | MUST |
| `session` | echo rule | echo rule — and §5.2 makes the request carry it | echo rule — and §5.2 makes the request carry it |
| `reason`, `error` | MUST, on `failure`/`unsupported` | MUST, on `failure`/`unsupported` | MUST, on `failure`/`unsupported` |
| `program` | MUST (§7.1.1) | MAY | MAY |
| `context` | MUST NOT | MUST, on success | MUST, on success |
| `artifacts` | MUST NOT | MAY | MUST, on success |
| `layers` | MUST NOT | MUST NOT | MUST, on success, for every patched layer |

The rows qualified "on success" are the ones that report *measured*
work. An invocation that failed before it got that far reports what it
measured and nothing more: a `build` refused with
`unsupported.required` has no artifacts, and a `verify` that could not
read `manifest.yaml` has no effective context ID. Fabricating either
would be worse than omitting it, since the backend compares both
against its own values (§5.3, §9.3). Everything not qualified is
mandatory unconditionally.

`program` in a `describe` result is deliberately one of those, and the
consequence is meant: **a `describe` that fails carries the block
too.** That is the case the block exists for rather than an oversight
in the qualification — a `describe` refused with `unsupported.request`
is refused because the backend sent a request version this program does
not parse, and `program.request` is the field that tells it which
version to send instead. A refusal that withheld the block would leave
the backend with nothing to correct its next invocation from, which is
the one thing `describe` is for. Nothing is fabricated by reporting it
either: every field of §7.1.1 is a property of the program itself, so
all of them are answerable whatever the request document said.

A **`cancelled` result carries `reason: null` and `error: null`**, just
as a successful one does, and it is the only status other than `success`
for which the two are null rather than mandatory. `status: "cancelled"`
already says everything there is
to say: the request document named a `cancel` file, the file appeared,
and the program stopped (§8). Nothing was diagnosed, so there is nothing
to classify — a registry value for it would be a second spelling of the
status.

`result` and `status` carry the whole document in every action because
they are its immortal preamble: they exist in every future result format
version, which is what makes an old program's refusal legible to a newer
backend, symmetrically to the request document's `{request, result}`.

**The echo rule: a program echoes what it was given, and only that.**
`action` is always echoed, because it is always present — it is
`argv[1]` (§5.1). `session` is echoed whenever the request document
carried it, which §5.2 makes mandatory for every working action — so a
`verify` and a `build` result carry it in every conforming invocation,
and the table states those two rows conditionally on purpose: a request
that omits it is the backend's breach of §5.2, and the echo rule wins
over the mandate it broke. `describe` gets by on the preamble alone, so
a backend that invokes `describe` without it MUST NOT expect it back,
and a program MUST NOT invent a value for a field it was never given.
Stating the rule this way keeps the smallest conforming `describe`
implementation possible — read two fields, write four — which is the
point of `describe` being the first conformance test (§7.1).

`context` is mandatory for both working actions and forbidden for
`describe`. Both `verify` and `build` are defined over the materialized
context, so both can compute its effective ID and both must report it
for comparison (§3.3). `describe` never touches a context — it is not
even guaranteed to have been given one — so it has nothing to compute
the ID from, and a `describe` result carrying one would be reporting a
value it could not have measured. This is the one asymmetry worth
naming explicitly, because a backend that demanded a context ID from
every result would make `describe` unimplementable in the very case it
exists for: a fresh, unknown image, asked what it can do, before any
context has been sent.

`layers` is mandatory for a successful `build` and **forbidden for
`verify`**, for the same reason `context` is forbidden for `describe`:
it reports work that was actually done, and `verify` does not do that
work. `verify` applies no patches and touches no source tree (§7.3), so
it has no applied patch set to report; a `layers` block in a `verify`
result would either restate the context's `patches/` directory — which
the backend already holds — or claim an application that never happened.
Both are worse than the field being absent, because the backend compares
the block against what it expects to have been applied.

`artifacts` is mandatory for a successful `build`, because a build that
declares nothing has produced nothing the backend may serve: the backend
serves exactly the intersection of declared and verified (§9.3), so an
absent list is an empty delivery, not a permissive one. `verify`
produces no artifacts of its own but is not forbidden from declaring
diagnostic output.

- `result` — the result format version; this document defines
  version 1.
- `status` — the enumerated set `success` | `failure` | `unsupported` |
  `cancelled`. Consumers MUST treat unknown values as `failure`.
- `reason` — a dotted value from an append-only registry owned by the
  MCUHome project; `x-*` for third parties. Unknown values are handled
  as their status class and passed through verbatim. Contract v1 defines
  twelve:

  | Value | The invocation |
  |---|---|
  | `unsupported.request` | cannot read the request document as specified: a `request` version it does not implement, a mandatory field it did not find, or a path value other than `result` that is not absolute (§5.2) |
  | `unsupported.required` | was told to honour a pointer or a value it does not honour; the pointers in `error.details.required` (§5.2) |
  | `unsupported.action` | asked for an action this program does not implement (§7) |
  | `unsupported.context` | found a `context` format version it does not implement (§3.2) |
  | `error.context.incomplete` | is missing a file the action needs, such as `keys/signing.pub` for a `build`; the missing path in `error.details` (§7.2) |
  | `error.context.unreadable` | found `manifest.yaml` and cannot read it as one: broken YAML, a missing section, or a hash in a spelling §3.3.1 refuses (§7.3) |
  | `error.context.mismatch` | measured the materialized context and it is not the context `manifest.yaml` describes — the `verify` failure (§7.3) |
  | `error.layer.unknown` | found `patches/<layer>/` for a layer it has no `trees` entry for or does not know (§6.2) |
  | `error.patch.incomplete` | found a layer recorded as started but not complete; terminal for the session (§6.2) |
  | `error.work.foreign` | found a `work` directory marked for a different session (§6.3) |
  | `error.build.failed` | did the work and the work did not produce what it was asked for — reaching or running code generation (§6.1), the compiler, the linker, artifact collection |
  | `error.deadline.exceeded` | stopped itself at `limits.deadline_seconds` (§5.2) |
  | `error.internal` | the program itself failed — an unexpected error inside any action, not a fact about the request or the context (erratum, 2026-08-11) |

  `error.build.failed` is the ordinary one and covers every failure of
  the build itself, including a failed `generate` child (§6.1): a
  compiler diagnostic is not a classification a frozen registry can
  enumerate, it is text, and text belongs in the log stream and in
  `error.message`. `error.deadline.exceeded` exists because
  `deadline_seconds` is advisory: enforcement is the backend's (§9.1),
  and a program that honours it anyway would otherwise have no typed way
  to say why it stopped. `error.internal` (erratum, 2026-08-11) is the
  one reason that is a fact about the *program* rather than the work or
  the request: an unexpected exception inside any action — `describe`
  and `verify` included — so a backend is never told a crash outside a
  build was a build-work failure. `error.build.failed` stays scoped to
  the build; the two are not interchangeable.
- `error` — the detail carrier: `{retryable, message, details}`.
  Mandatory whenever `status` is `failure` or `unsupported`, together
  with `reason`, and `null` otherwise. It carries no classification of
  its own — `reason` is the field consumers match on, and §5.4.1 states
  what the three subfields are for.
- `context` — the effective context ID actually worked on (§3.3),
  computed by the program from the context as materialized. It exists
  **for comparison only**: attribution always uses the backend's own
  independently computed ID.
- `artifacts[]` — **`root`, `path`, `role` and `hashes` are mandatory in
  every entry**, and each for a reason this document already states
  elsewhere: §5.3 makes an invocation successful only if every declared
  artifact exists as a regular file under its declared `root` and
  re-hashes to its declared value, which is unanswerable without the
  first, the second and the fourth; and `role` is the only thing that
  identifies an artifact by function, which is what a backend serves on.
  An entry missing any of the four is not resolvable, and a consumer
  MUST skip it exactly as it skips an unknown `root`.
- `artifacts[].root` names the request field the path is relative to.
  In v1 it has exactly one legal value, `"out"`. A
  consumer that sees a `root` it does not know MUST skip that artifact
  and MUST NOT resolve it against `out`. This is what makes a second
  output location possible later without silent mis-resolution.
- `artifacts[].path` is relative to that root, with path segments
  matching `[A-Za-z0-9._-]+`.
- `artifacts[].role` identifies the artifact by function rather than by
  filename, because a third-party image does not call its output
  `firmware.hex`. Append-only registry: `firmware`, `bootloader`,
  `combined`, `symbols`, `map`, `report`, `log`; `x-*` for third
  parties. **There is no `ota` role in v1** (§7.2).
- `artifacts[].hashes` is an object keyed by algorithm name, so a
  second algorithm is a sibling key rather than a format change, and so
  metadata names and algorithm names never share a namespace. Hashes
  MUST be read back from disk after `fsync`, never taken from a buffer.
  The key names the algorithm, so the value is bare — 64 lowercase hex
  digits for `sha256`, no prefix (§3.3.1).
- `layers[<name>].patchset` is defined exactly, otherwise a
  cross-implementation audit is worthless:

  ```
  SHA-256( "mcuhome-patchset-1\n"
           + for each file under patches/<layer>/, ascending byte order:
               <64 hex chars of the file's SHA-256> + " " + <filename> + "\n" )
  ```

  The value carries its own algorithm, so it is rendered `sha256:` +
  64 lowercase hex digits, and each `<64 hex chars>` inside the input is
  lowercase (§3.3.1).

New fields may be added to the result document; consumers MUST ignore
fields they do not understand. Incompatible changes require a `result`
format-version bump.

#### 5.4.1 `reason`, and what `error` carries

`reason` is the classification and the only field a consumer matches on:
it is the program's statement about its own invocation, taken from the
append-only registry above. `error` stands beside it as a carrier with
three subfields and no classification of its own.

- **`error.retryable` is the program's promise about its own failure**,
  and about nothing else: it says whether re-running *this invocation*,
  unchanged, could succeed. A backend MUST NOT relay it as the session
  protocol's `retryable` — that value is the server's, derived from the
  server's own registry precisely so the promise cannot be forged
  (`build-server/mcuhome_buildserver/errors.py:13-17`, `:199-216`), and
  the two answer different questions.
- **`error.message` is untrusted text**, and the only untrusted-text
  field in the document: backends and clients MUST NOT render it raw
  into a context where markup or control characters matter.
- **`error.details`** is structured and reason-specific. Contract v1
  fixes its contents only where a `reason` says so —
  `error.details.required` for `unsupported.required` (§5.2).

**Embedding a result into ADR 0019 §3's error envelope is the backend's
business.** The envelope's fields are derived from `reason`, and *how*
is deliberately not frozen here: that envelope belongs to the session
protocol and classifies *protocol operations*, most of which have no
invocation behind them at all, while `reason` classifies *invocations*,
which is the only thing a program can speak about. Fixing the mapping in
this contract would have frozen one value in three places — `reason`,
the envelope's `code`, and the code's dotted prefix — to serve a
consumer this contract does not have.

## 6. The build environment, and patched layers

### 6.1 The program assembles its own build environment

**Assembling a working build environment — the west workspace, module
registration, `ZEPHYR_BASE`, `CHIP_ROOT` — is the program's
responsibility, out of `context`, `trees` and `work`. The backend never
supplies a workspace.**

Four paths do not make a build. The CMakeLists MCUHome generates
resolves `${ZEPHYR_MCUHOME_MODULE_DIR}` (`mcuhome/compiler/generate.py:1343`,
`:1354`), which is defined only inside a registered Zephyr module tree;
it finds Zephyr via `find_package(Zephyr REQUIRED HINTS
$ENV{ZEPHYR_BASE})` (`:1331`); and it searches for `CHIP_ROOT` via
`$ENV{ZEPHYR_BASE}/../modules/lib/connectedhomeip`, falling back to an
upward search for `modules/lib/connectedhomeip`
(`mcuhome/compiler/generate.py:1200-1215`).

The contract assigns this responsibility rather than freezing a
topology, because freezing topology fields would freeze *MCUHome's*
layout onto every third-party image. An NCS-based image with a
completely different layout satisfies the assignment; it could not
satisfy a frozen topology.

How MCUHome's own build container discharges it, as a worked example
and not as a requirement: the image bakes a real west workspace
(`.west/config`, `zephyr/`, `modules/`, `bootloader/`) at image-build
time, using the same `west init -l` + `west update` that CI already
runs. The program registers the tree it is handed as `trees.sdk` as the
MCUHome Zephyr module inside that workspace; the module declares its
own name in its `zephyr/module.yml`, so registration does not depend on
which path the backend chose. That keeps `west build --sysbuild`,
keeps the generated CMakeLists' CHIP discovery working, and means the
program has to set nothing the workspace does not already answer.

**Code generation runs here.** The per-device Zephyr application tree
is generated inside the build container, by the compiler package
shipped in the SDK package that arrives as `trees.sdk`. A conforming
build container therefore **MUST** execute MCUHome's code generation
out of the SDK tree it is given. Stated plainly: *"bring your own build
container" means your own toolchain and your own Zephyr, not your own
build logic.* A container that generates the application differently
does not produce MCUHome firmware, and no context ID would describe
what it built.

**How code generation is reached: a defined entry point in the SDK
package.** The SDK package ships an **executable entry point**, and the
program invokes it **as a child process**. That is the whole interface:
no import, no linkage, no in-process API, and therefore no language
requirement on the caller. A build container written in Rust, Go or
shell starts a process and reads its exit status like any other tool it
drives.

**Where the entry point is declared — normative.** At the root of the
tree the backend hands over as `trees.sdk` there is a file named
`mcuhome-sdk.json`: one JSON object, UTF-8 without BOM, RFC 8259, read
with the JSON parser §5.1 already requires and nothing more. Contract v1
fixes three names in it and no values:

| Field | Meaning |
|---|---|
| `sdk` | the metadata format version, an integer; this document describes version 1 |
| `generate.program` | the entry point, as a path relative to the root of `trees.sdk` |
| `generate.runtime` | an opaque string naming the runtime the entry point needs |

Unknown fields are ignored, as everywhere else in this contract. A
missing file, a missing field and a `sdk` version the program does not
implement are all one situation — code generation cannot be reached —
and all three fail the invocation with `reason: "error.build.failed"`
(§5.4). They are not `unsupported`: the program implements everything
this contract asks of it, and no other container would fare better with
this SDK package.

Fixing the location and the field names is the whole addition; the
values stay the SDK package's business, versioned with the package
(ADR 0020 decision 8: one release, one tag, one version for the
packages, the SDK archive and the image). It is also what keeps §3.1's
promise: the entry point is found from the tree the backend handed over,
under names this contract states, so reaching code generation is not
out-of-band knowledge and nothing about it is compiled into an image.

**How the entry point is invoked: the ABI the program already speaks.**

```
<trees.sdk.path>/<generate.program> generate <absolute path of a request document>
```

That is §5.1 unchanged — two positional operands, the request document
of §5.2, the result document of §5.4, the exit codes of §5.3. The
program writes that request document into its own `tmp` and is the
*backend* of that invocation, in exactly the sense §1.1 defines.
`generate` is an action of the SDK entry point and **not** of the
program: it is never invoked on `/mcuhome/run` and never appears in
`program.actions`.

Reusing the invocation ABI is the point of choosing it. A second calling
convention would be a second frozen thing — its own operand order, its
own error channel, its own exit values — for a caller that already
implements the first one; this way the entry point is reached with the
parser, the two documents and the four exit values every conforming
program has anyway.

Two things about the invocation are fixed here because the program has
to know where to look afterwards: the entry point reads the build
context from `context` and writes the per-device Zephyr application tree
into `out`. Everything else in the document is between the SDK package
and itself. A non-zero exit, a missing result document or a `status`
other than `success` fails the invocation with
`reason: "error.build.failed"` (§5.4).

The honest consequence, stated rather than hidden: **a conforming build
container MUST provide the runtime named in `generate.runtime`.** Today
that value names a Python interpreter, because code
generation is written in Python — but it is a property of the SDK
package, declared by the SDK package, and not a clause of a frozen
contract. If the implementation language ever changes, the SDK package
declares a different runtime and **no third-party container breaks
against this contract**: the invocation shape it implements (§5.1), the
documents it exchanges (§5.2, §5.4) and the responsibility it discharges
(this section) are all unchanged, and its own build logic is not
involved either way. An image that does not carry the newly declared
runtime cannot serve that SDK release — but that is a package it adds,
not a specification it has to chase, and it learns of it from the
metadata rather than from a child process that failed in the middle of a
build.

The alternative — naming the interpreter, the module path or the
function in this document — was rejected for the reason the whole
contract exists: it is frozen, so it would freeze MCUHome's
implementation language onto every third party forever, and it would do
so to specify something a child process already specifies exactly.

### 6.2 Patched layers: writable views, applied once

A layer that carries patches (files under `patches/<layer>/`) needs a
writable source tree. The contract guarantees the **behavior**, never
the mechanism:

- The **backend** MUST provide a *writable view* of each patched layer
  and MUST name it in `trees` with `writable: true` (§4.1). **In the
  `container` profile the container's own copy-on-write layer is that
  view, and it costs nothing to provide**: the image's trees are
  writable inside the container by construction, one session is one
  container (ADR 0019 §2), and the container is discarded at
  `close-session` — so a patched `zephyr` never outlives the session
  that patched it, which is the whole isolation the view exists for.
  The backend asserts `writable: true` for an in-image tree at the path
  `describe` reported, and the assertion is truthful because the layer
  makes it so. No overlay is mounted and no copy is made.
- The `subprocess` profile has no container layer, so there the view is
  the backend's to construct — a copy-on-write overlay on the host
  (lowerdir = the pristine read-only source), or a copy as the
  conforming fallback. An overlay, where used, MUST be constructed
  outside any process that executes untrusted patch code: such a
  process MUST NOT hold the mount privileges (CAP_SYS_ADMIN) that
  overlay mounting requires. And because this profile's build
  environment is persistent rather than discarded, patches mutate it
  durably — which is why patch support there is opt-in, actively
  configured, at the operator's own risk (ADR 0019).
- The **program** MUST apply the patches of each patched layer to its
  view, in the order of §3.1, before building. It applies them **once
  per session**: the patch set of a locked context cannot change, so
  there is no comparison to perform and nothing to reapply. The format
  and the application semantics are fixed below.
- **Patch application belongs to `build` and to `build` alone.** A
  `verify` MUST NOT apply patches, MUST NOT write to a layer view, and
  MUST NOT create or update the per-layer records described below; it
  checks the materialized context against the integrity list and touches
  no source tree at all (§7.3). A session whose first working action is
  `verify` therefore still has pristine trees when the first `build`
  runs, and "applied once per session" is unaffected — the record in
  `work` is what marks the application, and only `build` writes it.
- The program MUST record per layer, in `work`, that it started
  applying that layer's patches, and MUST record that the application
  completed only after the last patch of that layer applied cleanly. On
  a later invocation of the same session it MUST NOT reapply a layer
  recorded as complete.
- If the program finds a layer recorded as started but not complete, it
  MUST fail the invocation with `reason: "error.patch.incomplete"`, and
  the backend MUST refuse every further working action in that session.
  The client's remedy is a **new session** — a new container, hence
  pristine trees.
- If `patches/<layer>/` names a layer for which there is no `trees`
  entry, or which the program does not know, the program MUST NOT
  proceed: `status: "failure"`, `reason: "error.layer.unknown"`.

**Patch format and application semantics.** A patch file is a unified
diff. It is applied with **`-p1` semantics relative to the root of the
layer's tree**: the first path component of every path in the diff is
stripped, and what remains is resolved against `trees.<layer>.path`.
The patches of a layer are applied in the ascending lexicographic
filename order §3.1 already fixes (the `NNNN-` prefix convention,
ADR 0018 decision 2), each on top of the result of the previous one.

Which tool does the applying is not part of the contract: `git apply`,
`patch -p1`, or a diff implementation the program brings itself are all
conforming, and a third-party program in another language does it its
own way. What the contract fixes is the semantics — the same tree, from
the same patch files, in the same order, whichever tool produced it.
Without a stated strip level and a stated root a third-party
implementer has to guess one, and a patch generated by `git diff`, with
its `a/`…`b/` prefixes, would apply in one implementation and fail in
the next. Because the strip level is fixed, a patch that does not apply
is a failure of the invocation and not something to search around: a
program MUST NOT retry the same patch at a different strip level or
against a different root.

**There is no layer reset in this contract, and no `generation`
counter.** Both existed to handle a patch set changing between
invocations of one session. `lock-context` (ADR 0019 §2) makes that
unreachable: patches can only arrive before the context is locked, and
no working action runs before the lock, so the patch set is constant
for the whole life of a locked context. With the triggering condition
gone, the backend's duty to reset a view, the program's duty to record
and compare a patch-set identity on every invocation, and the per-tree
generation counter that would have detected a reset all go with it.

An interrupted patch application — a crash, a cancellation or an
out-of-memory kill after some patches but before all — is therefore
**terminal for the session**, not recoverable in place. Two reasons.
First, a retry would apply exactly the same patches: if a patch is
broken it fails identically, and if it was a crash, a new session costs
a container start and a cold build; a recovery mechanism would be two
frozen contract obligations for a case a new session already resolves.
Second, restoring the pristine baseline is not possible from inside the
merged view at all — deleting a file there creates a whiteout instead
of restoring the base (ADR 0019 §5), so the program cannot clean up
after itself even if it wanted to.

Incremental build state survives only for untouched layers.

### 6.3 A foreign session marker in `work`

`work` is the session's persistent working area and the backend
guarantees it is **exclusive to this session** (§4, §9.1). §5.2 names
the one thing `session` is for beyond being echoed — letting the program
recognise a `work` directory left behind by a different session — and
§9.1 permits the marker that makes that possible. This section says what
happens when it fires.

- Writing a marker is **optional**: a program MAY record its `session`
  in `work` on first use. A program that records none has no guard, and
  that is conforming — the exclusivity is the backend's duty, not the
  program's to prove.
- A program that records a marker **MUST read it before using anything
  in `work`**, on every invocation. A guard that is written and not read
  is worse than no guard, because it looks like one.
- **Absence of a marker means nothing.** An empty `work`, a `work`
  holding state but no marker, and a `work` written by a program that
  never marks are indistinguishable, so a program MUST NOT conclude
  "this is mine" from a missing marker. It concludes only "no prior
  state of this session", which §7.2 already defines: an `incremental`
  in that situation is executed as `clean`.
- **A marker naming a different session is terminal for the
  invocation.** The program MUST fail it — `status: "failure"`,
  `reason: "error.work.foreign"`, the two session IDs in
  `error.details` — and it MUST NOT do any of the three things that
  would otherwise be tempting: it MUST NOT use the state it found, MUST
  NOT delete or overwrite it, and MUST NOT fall back to a private
  working area of its own choosing. It writes nothing into `work` in
  this case, not even its own marker.
- **The backend MUST treat it as a defect on its own side**, because it
  is one: the marker can only differ if the exclusivity guarantee of
  §9.1 was broken. It MUST NOT retry the invocation against the same
  `work`, and it MUST refuse every further working action in that
  session; the remedy is a new session with a `work` directory the
  backend actually owns.

The three refused alternatives are refused for one reason each, and they
are why this is a failure rather than a recovery. Reusing the state
builds against another session's tree and attributes the result to this
session's context ID — a wrong artifact that looks right. Deleting it
destroys the other session's build, concurrently and silently, and §9.1
does not promise the other session has stopped. Choosing a different
working area hides a broken backend behind a slow build, and hides it
permanently, because nothing else in the system would ever notice.
Failing typed is the only outcome that reaches the operator, and it
costs a session that was already unsafe. This mirrors §6.2's
interrupted patch application, deliberately: both are cases where the
environment is not what the contract says it is, and in both the
contract buys a new session rather than a repair mechanism.

## 7. Actions

Contract v1 defines `describe`, `build` and `verify`. A program MAY
implement further actions; it MUST announce its full set in
`program.actions`, which is the only channel they are announced on
(§2.1). An action a program does not implement produces
`status: "unsupported"`, `reason: "unsupported.action"`, exit
1 — a legible refusal the backend can reschedule on, which is exactly
what the removed exit code 64 could not deliver.

### 7.1 `describe`

Mandatory. It needs only `request` and `result`, never touches the
context, writes nothing but the result document, and fills the
`program` block of §5.4. Because it touches no context it reports no
context ID, and it echoes only the fields the request actually carried
— §5.4's echo rule exists for exactly this action.

`describe` is **authoritative** about what the program can do. The
image labels (§2.1) are a pre-start hint; a backend MUST verify them
against `describe` and MUST NOT rely on a label `describe` contradicts.
An image MAY also carry the same answer statically at
`/mcuhome/describe.json` (§2.2.1), which is a pre-start hint under
exactly that rule and never a replacement for this action: it exists for
the image whose program body arrives with a mounted tree, and it is
therefore read *before* a mount point is chosen and checked here
afterwards.
It is also the only discovery channel that exists in the `subprocess`
profile, where there is no image and therefore no labels — and the only
way a backend learns where a foreign image keeps its trees, without
which §6.2's writable views cannot be arranged at all. It doubles as
the first conformance test: a program that cannot answer `describe`
cannot be trusted with a build.

#### 7.1.1 The `program` block, field by field

The block is defined here and not only by the example in §5.4. Whenever
`program` is present — mandatory in a `describe` result, optional in any
other — **every field below is mandatory inside it** except `trees`,
which is mandatory only in a `describe` result. A `describe` result
whose `program` block is missing one of them is a failed `describe` —
each field answers a question the backend has to answer before it can
invoke anything. In a `verify` or `build` result, where the whole block
is optional, an incomplete one is a contract violation against the image
and MUST NOT be used as discovery data; the backend asks `describe`.

- **`id`** — a stable identifier of the *implementation*, not of an
  image, a tag, a version or a vendor. Reverse-DNS (MCUHome's own is
  `org.mcuhome.build-container`); third parties use their own domain.
  It is opaque: a backend compares it for equality and does nothing
  else with it. What it may conclude is exactly one thing — two programs
  reporting the same `id` are the same implementation, which is what
  makes it usable as the ccache subdirectory name §10 recommends. It
  says nothing about capability (`actions` does) and nothing about
  trust.
- **`version`** — the implementation's own version, as a string, opaque
  to the backend. MCUHome's own program reports the single shared
  release version of ADR 0020 decision 8 — the one number that covers
  the packages, the SDK archive and the image — but a third party's
  version scheme is its own business. A backend MAY log it, put it in a
  build report and quote it in a bug report; it MUST NOT parse it and
  MUST NOT make a compatibility decision from it. Compatibility is
  decided by `contract`, `request`, `result`, `actions` and the label
  constraint of §2.1.1, all of which are declarations rather than
  inferences.
- **`contract`** — the contract version this program implements, as an
  integer. This document specifies version 1. In the `container` profile
  it MUST equal the `org.mcuhome.contract` label; where the two
  disagree, `describe` is authoritative and the disagreement is a
  contract violation against the image (§2.1). A backend that does not
  implement the value it finds here MUST NOT invoke a working action on
  this program — everything else in the result document is described by
  a specification the backend does not have.
- **`request`** — the request format versions the program can parse, as
  a non-empty array of integers. The backend MUST write a request
  document whose `request` value is one of them; a value outside the
  list is answered `unsupported.request` (§5.2 rule 3), so this field
  exists to make that refusal avoidable rather than discoverable.
- **`result`** — the result format versions the program can write, as a
  non-empty array of integers. The backend must implement at least one
  of them, or it cannot read what this program produces. There is no
  negotiation and none is needed: the version actually used is the one
  the result document itself declares in its immortal preamble, and
  §5.3 already makes a result document naming an unimplemented `result`
  version a failed invocation. `program.result` is the advance notice of
  that, not a second channel for it.
- **`actions`** — the action names the program implements, as an array
  of strings, and exactly those: it MUST list every action it
  implements and MUST NOT list one it does not. It is the **only**
  declaration of the action set; no image label carries one (§2.1). A
  backend MUST NOT invoke an action absent from the list; if it does
  anyway, the legible answer is `unsupported.action`, exit 1.
  **Conformance is claimed by the declared contract version and never by
  this list**: by the `org.mcuhome.contract` label (§2.1), and by the
  `contract` field above it where there is no image to carry a label.
  Contract v1 requires all three actions of §7 of a conforming program,
  so a program implementing fewer is a legible non-conforming program
  rather than a broken one, and the short list is the correct thing for
  it to report — the list is what a backend acts on, so one that claimed
  an action the program does not have would be the single lie a backend
  cannot catch before invoking it.
- **`trees`** — where the image keeps each layer it carries, and where
  it requires one to be put, as a map from layer name (§1.1) to an
  object with a mandatory `path` and an optional `version`. Mandatory in
  a `describe` result: it is the only way a backend learns where a
  foreign image keeps its trees, without which §6.2's writable views
  cannot be arranged at all. A `path` of `null` means "this tree is not
  in my image; put it wherever you like and name it in `trees`" — which
  is why `version` is optional, since an image that does not carry a
  tree cannot state its version (the §5.4 example shows exactly that for
  `sdk`). A concrete path is where the image keeps that tree, and for a
  tree the backend supplies it is **the path the backend MUST supply it
  at** (§4): a program that names a path for a layer it does not carry
  is stating a requirement, not describing its own filesystem, and a
  backend naming any other path in `trees.<layer>` is naming one the
  program cannot honour — `unsupported.required` where the pointer was
  demanded through `required` (§5.2 rule 2), and a failed invocation
  otherwise. The distinction a backend needs is therefore in the value
  and not in a second field: `null` asks, a path requires.

Taken together, `contract`, `request`, `result`, `actions` and `trees`
are everything a backend needs to arrange an invocation without having
attempted one; `id` and `version` are everything it needs to attribute
what came back. That division is deliberate: nothing in the block has to
be interpreted, and nothing in it may be inferred from anything else.

### 7.2 `build`

Parameter `mode`, from `params` in the request document. **The default
is `clean`**: an absent `params`, a `params` without `mode` and an empty
`params` all mean `clean` (§5.2), because the safe default is the one
that never silently reuses state.

- `clean` — fresh workspace. Required for release artifacts
  (reproducibility and attribution). This is what the program does when
  the request document names no mode.
- `incremental` — warm workspace; results are session-private. An
  `incremental` for which the program finds no prior state of *this
  session* in `work` is executed as `clean`. This replaces contract v1's
  original "the first build in a freshly materialized container counts
  as clean", which named no predicate a program could evaluate — and
  container materialization is lazy anyway (ADR 0019 §2).

A successful device build MUST declare at least two artifacts: the
unsigned image with role `firmware` (MCUHome's own container writes
`firmware.hex` and `firmware.bin`), and **exactly one artifact with role
`report`**, whose content is the build report of §7.2.1 (MCUHome's own
container writes `build-report.json`). The report is mandatory because
the program is forbidden to sign and the client therefore has to: a
build whose parameters the client cannot read produces an image nobody
can sign.

There is **no `ota` role in v1.** Contract v1 as first drafted listed
"the unsigned OTA payload" among the expected artifacts; it is
unbuildable as specified and is struck. The OTA wrapper's payload "has
to be the **signed** binary" (`mcuhome/workbench/otafile.py:154-160`) and the same
contract forbids the program to sign, so the requirement cancelled
itself. A gap is better than a frozen contradiction.

The program MUST NOT sign images: reproducibility covers the unsigned
image, and signing is detached and client-side by design (ADR 0015
decision 8). It MUST use `keys/signing.pub` from the context as the
bootloader's verification key and MUST NOT fall back to a default
signing key.

**`keys/signing.pub` is therefore required for `build`.** A context
submitted to a `build` that does not carry it fails the invocation
typed — `status: "failure"`, `reason: "error.context.incomplete"`, the
missing path in `error.details` — and the program MUST NOT build
anyway. There is no fallback to MCUboot's
default key, because that default is MCUboot's demo key
(`mcuhome/compiler/workspace.py:572-575`) and **its private half is published**:
the key MCUboot's Kconfig names as the default,
`root-ec-p256.pem` (`bootloader/mcuboot/boot/zephyr/Kconfig:471`), is a
PEM private key checked into the MCUboot repository. Firmware built
against it accepts an update signed by anyone who cloned MCUboot. A
silent fallback would therefore turn a forgotten file into an
unauthenticated update path, and the resulting image is
indistinguishable from a correct one by inspection — which is why the
absence has to be an error and not a default.

The requirement is scoped to `build` alone. `verify` compares the
materialized file set against the integrity list and `describe` does
not touch the context at all (§7.1, §7.3); neither needs a verification
key, and neither may refuse a context for the absence of one. A context
that will only ever be verified or described is complete without
`keys/signing.pub`.

#### 7.2.1 The build report

The `report` artifact is one JSON object, UTF-8 without BOM, RFC 8259.
It exists for one consumer and one purpose: the client that signs
detached (ADR 0015 decision 8, dashboard ADR 0007 decision 3), which is
the only party holding the private key.

```jsonc
{
  "report": 1,                       // report format version

  "signing": {                       // MANDATORY
    "signature_type": "ecdsa-p256",
    "arguments": {                   // imgtool's own option names
      "version":     "1.4.0+0",
      "header-size": 512,
      "align":       4,
      "slot-size":   983040
    }
  },

  "memory": [                        // OPTIONAL
    {"image": "mcuboot", "region": "FLASH",
     "used": 49152, "total": 65536, "percent": 75.0}
  ]
}
```

- `report` — the report format version, an integer; this document
  describes version 1. A consumer that does not implement the version it
  finds MUST NOT sign from the document.
- `signing.arguments` — the **four `imgtool sign` arguments**, under
  imgtool's own option names, so the block reads as the command it
  stands for: `version` is imgtool's `major.minor.revision+build` string,
  which MCUboot compares monotonically; `header-size` and `slot-size` are
  byte counts; `align` is the write block size. These are exactly the
  four the reference implementation carries and the four its signer
  passes (`mcuhome/model/manifest.py:208-230`, `mcuhome/workbench/imgtool.py:155-162`),
  and three of them are board data the build already had to know
  (ADR 0015 decision 2) while `version` comes from the built
  application's own Kconfig (`mcuhome/compiler/report.py:100-135`).
- `signing.signature_type` — the key type MCUboot was configured to
  verify with, so a client can refuse a mismatched key instead of
  producing an unbootable image. MCUHome's own value is `ecdsa-p256`
  (`mcuhome/model/registry.py:332-336`).
- The parameters apply to **every** artifact declared with role
  `firmware`. There is one unsigned image, and a build may declare it in
  more than one encoding — MCUHome's own container writes `firmware.hex`
  to flash and `firmware.bin` to sign (§7.2) — so the same four
  arguments describe each of them, and a per-artifact block would be the
  same block repeated. Nothing names any of those artifacts a second
  time: `role` is already how an artifact is identified (§5.4).
- `memory` — optional, one entry per memory region of a linked image,
  with exactly the fields of the `build.memory.region` event (§8):
  `image`, `region`, `used`, `total`, `percent`. It is the footprint
  table the linker actually enforced, parsed rather than recomputed
  (`mcuhome/compiler/workspace.py:881-889`, parser at `:890-903`, image
  attribution at `:913-952`). A build that relinked nothing reports none,
  which is correct rather than incomplete.

**The report format is versioned by `report` and is deliberately not
part of the frozen surface.** It evolves on its own version, independent
of the contract version, of the `request` version and of the `result`
version — which is why what it carries could be cut to what a signer
actually needs. Everything §7.2 previously promised here and no consumer
asked for is gone: the container digest and the effective context ID are
values the backend computed itself and MUST use its own copy of anyway
(§9.3); warnings are log text (§8); ccache statistics had no named
reader; and the reference implementation's `signed`, `signed_by_the_build`,
`inputs` and `outputs` (`mcuhome/model/manifest.py:273-288`) are all decided
elsewhere — a build container never signs (§9.2 point 6), the input is
the `firmware` artifact, and where the signed output goes is the
signer's business, on the signer's machine.

Unknown fields are ignored, as everywhere else in this contract, and a
program MAY add its own.

### 7.3 `verify`

Asserts that the materialized context is the context the manifest
describes. It checks the **effective** context — the file set as
materialized, against the integrity list in `manifest.yaml`, which
`lock-context` wrote over exactly that file set — and reports the
resulting `context` ID in its result. A file that is missing, a file
whose bytes hash to something else, and a file present but absent from
the list are one outcome and one typed answer:
`status: "failure"`, `reason: "error.context.mismatch"`, the offending
paths in `error.details`.

A `manifest.yaml` that is present and cannot be read as one — YAML that
does not parse, a section the format requires and the document does not
have, a hash rendered in a spelling §3.3.1 refuses — is a different
failure and carries its own answer, here and wherever else an action
has to read the manifest: `status: "failure"`,
`reason: "error.context.unreadable"`. It is not
`error.context.mismatch`, because nothing was measured against
anything: a document that describes no context cannot be disagreed
with, and a backend told "mismatch" would go looking for the tampered
file that does not exist. It is not `error.context.incomplete` either,
since the file is there; and it is not `unsupported.context`, which is
reserved for a format version the program does not implement and says
of it that "nothing about this context is broken" (§3.2). A manifest
that states no `context` format version at all is therefore unreadable
rather than unsupported — no other image would fare better with it, so
there is nothing for a backend to reschedule onto.

**`verify` does not apply patches, and it touches no source tree.** It
reads the context and nothing else: it does not write into any `trees`
entry, does not create or update the per-layer patch records in `work`
(§6.2), and does not report `layers` (§5.4). Patching is part of
building, and a `verify` that patched would have three effects nobody
asked for — it would consume the "applied once per session" budget
before the first build, it would make a read-only check into a writing
one, and it would make a cheap pre-flight check cost a patch
application. The backend still supplies the writable views §4.1
requires for every patched layer — `verify` simply does not use them,
and a view it never writes to is indistinguishable from one it was not
given.

It must not be defined against a base header that predates the
extensions: under contract v1 as first drafted, `manifest.yaml` was
frozen for the session while the session protocol allowed extension, so
every added file was reported as "present but not in the integrity
list" and the check returned `ok == False` by construction
(`mcuhome/workbench/contextdir.py:623-631` builds the mismatches,
`:592-593` is the rule). `lock-context` closes that
by giving the integrity list a defined moment at which it is written:
after the last extension, before the first working action.

Deeper checks are implementation-defined, and they are read-only too: a
`verify` MUST NOT modify the context, a source tree, or `work`. Writing
diagnostic output into `out` is the one thing it may write, and §5.4
already permits that (`artifacts: MAY`).

`verify` is optional for callers; a backend fast path may never invoke
it. It is not a complete integrity check on its own — see §3.3 and
§9.1.

## 8. Logs, events and cancellation

**Logs.** Standard output and standard error together are one raw,
opaque log stream. Children inherit both descriptors. Consumers MUST
NOT parse the log stream for machine decisions, and a program MUST NOT
write anything to stdout that is meant to be parsed.

**Events.** Only if the request document carries `events`. The program
appends NDJSON to that file — one JSON object per line, UTF-8, flushed
after every line, append-only, never truncated. Every object carries
`"event": "<name>"` and a monotonic `"seq"` starting at 1. Event names
are an append-only registry; unknown names are passed through opaquely.
Lines longer than 8192 bytes and non-objects are discarded and counted
by the backend, never treated as an abort. A program MUST NOT block on
writing an event and MUST NOT die if the write fails. Where the two
obligations collide — a full pipe, a stalled disk — **not blocking
wins**: the program drops the event rather than wait, and "flushed after
every line" binds only the lines it does write. A dropped event leaves a
gap in `seq`, which is harmless because `seq` is only required to be
monotonic and a backend MUST NOT infer anything from an event it did not
receive (below).

**The event-name registry.** Names are dotted, `[a-z][a-z0-9.-]*`, with
`x-` for third parties, and the registry is **append-only**: a released
name is never renamed, never removed and never given a different
meaning — correcting a name means adding a new one and letting the old
one age out of use. Contract v1 seeds it with the phases a build
actually has. Each entry names the moment it is emitted and the fields
it carries beyond the two every event has (`event`, `seq`):

| Name | Emitted | Additional fields |
|---|---|---|
| `invocation.started` | once, as soon as the request document is understood and before any work | `action` |
| `context.checked` | once, after the effective context ID has been computed from the materialized files | `context` |
| `patch.layer.applied` | once per patched layer, after its last patch applied cleanly (§6.2) | `layer`, `count` |
| `generate.written` | once, after the per-device application tree has been generated into the workspace | `files` (count) |
| `build.image.started` | once per image of the sysbuild build, when its build begins | `image`, `current`, `total` |
| `build.memory.region` | once per memory region of a linked image, when the linker's footprint table is read | `image`, `region`, `used`, `total`, `percent` |
| `artifact.collected` | once per artifact the program will declare, after it exists on disk | `role`, `path`, `size` |
| `invocation.finished` | once, immediately before the result document is written | `status` |

These are the real phases, not a plausible-looking list. `generate.written`
is stage 4, which produces the application tree in one pure step and
writes it (`mcuhome/compiler/generate.py:1396`, `write_tree` at `:1430`) — one
event, because the step takes milliseconds and has no progress to
report. `build.image.started` is the only marker a sysbuild log actually
contains that says whose output follows: the outer build prints
`Performing build step for '<image>'`, and the reference implementation
matches exactly that to attribute everything after it
(`mcuhome/compiler/workspace.py:906-910`). `build.memory.region` is one row of
Zephyr's footprint table, which is parsed rather than recomputed because
it is the number the linker script actually enforced
(`mcuhome/compiler/workspace.py:854-889`), and it carries `image` because the
table itself names no image and is attributed by the banner above it
(`:913-952`). `artifact.collected` follows the collection step that
walks each image's output directory (`mcuhome/compiler/workspace.py:762-806`).
`context.checked`, `patch.layer.applied` and the two `invocation.*`
events are the contract's own phases — §3.3, §6.2 and §5.1 step 10.

Emitting events is optional in both directions: a backend that offers no
`events` file gets none, and a program that offers fewer names than the
table is conforming — the registry says what a name **means**, never
that it must occur. Two consequences follow, and they are what make an
append-only registry safe to grow. A backend MUST NOT infer anything
from a name it did not receive: an absent `build.memory.region` may mean
the program emits no events at all, or that an incremental build
relinked nothing — it never means the build produced no image, and the
result document is where that question is answered. And
**unknown names are relayed opaquely** — a backend passes an event whose
name it does not know through to its client verbatim, with its fields
intact, and never drops it, never rewrites it and never treats it as an
error. That is what lets a third-party program report its own phases
under `x-` names through a backend that has never heard of them.

Contract v1 as first drafted put NDJSON progress events on **stdout**
and logs on stderr. That makes file descriptor 1 correctness-bearing in
a process that starts west, cmake, ninja, gn and zap — all of which
write to stdout. A Go implementer writing the idiomatic `cmd.Stdout =
os.Stdout` corrupts the event stream and never notices locally;
MCUHome's own reference implementation already merges the two streams
(`mcuhome/compiler/workspace.py:730-731`), which is the same observation from
the other side. A named file removes the failure mode structurally,
survives an out-of-memory kill readably, and gives a reconnecting
client the resume-from-offset that ADR 0019 §2 requires anyway.

**Cancellation.** If the request document carries `cancel`, the
**existence** of that file means "stop". The program SHOULD poll it,
stop within `limits.cancel_grace_seconds`, and write a result with
`status: "cancelled"`. It is optional so that a fifty-line third-party
program stays possible; SIGTERM/SIGKILL remains the backend's hard
path. A cooperative sentinel is used rather than a signal because
killing a `docker exec` client does not kill the process inside the
container, and because the same mechanism works unchanged in the
`subprocess` profile.

## 9. Execution environment: guarantees and prohibitions

### 9.1 What the backend MUST enforce, and the program MAY rely on

These are duties of the **backend**, in both profiles. A backend
orchestrates a build; it is never itself the build environment. In the
`container` profile the environment is a container it starts; in the
`subprocess` profile it is a separate process running in the same
filesystem as the build server (§1.2). Neither shape moves a duty from
this list onto the program.

Two of the duties below are nevertheless enforceable only in the
`container` profile, and §1.2 says so from the other side: a
`subprocess`-profile backend has **no network isolation and no
per-session resource limits**. Everything a shared filesystem still
allows — materializing the context safely, verifying the SDK,
cross-checking the pins, keeping one invocation at a time per `work`,
hardening egress (§9.3) — is unchanged, because none of it depends on a
kernel namespace. The prohibitions on the program are unaffected too —
they are obligations, not observations — but a program MUST NOT infer
from a successful network call or an unenforced limit that it was
permitted either. Everything else in this section applies to both
profiles.

- **No network during an invocation.** The program MUST NOT require
  network access at any point; everything a build needs is in a tree,
  in the context, or in the image. External inputs are the backend's to
  fetch and to hand over as paths.
- **A verified SDK**: the content of `trees.sdk` matches the manifest's
  `mcuhome.package.sha256`. The backend acquires the package by (name,
  version, sha256) from operator-configured sources only; the
  manifest's `package.url` is a hint, never an instruction (ADR 0019
  §8).
- **A build environment of the required line.** The backend MUST run
  the invocation in a build container that serves the context's `zephyr`
  line (§3.2), and MUST record which one in `manifest.yaml`'s `container`
  block at `lock-context`. A backend that serves no such container MUST
  refuse, typed, and MUST NOT substitute a container of another line.
- **Cross-checked pins.** The backend MUST check `zephyr`,
  `mcuhome.package.sha256` and `target.board` from the manifest against
  what it **actually resolved, pulled and unpacked**, and against the
  header the session was admitted on. This is the duty that closes the
  gap named in §3.3: recomputing the context ID over the `files` list
  does not cover the other hashed inputs, so without this check a
  self-consistently forged manifest verifies clean — and `zephyr` is
  covered by nothing else at all, being hashed nowhere. Every comparison
  of a hash is a comparison of the exact spelling §3.3.1 fixes; a
  manifest whose hashes are rendered any other way is rejected before it
  is compared to anything. `container.digest` is deliberately **not** on
  this list: it is the backend's own record rather than a value the
  client sent, so comparing it against the pins would be comparing the
  backend to itself.
- **`zephyr` against the model that carries it** (erratum, 2026-08-11).
  The backend MUST check the context's `zephyr` against
  `toolchain.zephyr_line` in `model/device-model.json`, and MUST refuse
  when they disagree. §3.3 leaves `zephyr` out of the hash as
  "redundancy, not identity" *because* the line is provably inside the
  hashed model — and nothing made it provable: `context.yaml` is outside
  the integrity list by construction, so two contexts with identical
  files and two different required lines have one context ID, build in
  containers of two different lines, and both verify clean. This is the
  one field of the device model a backend reads, it is read out of a
  file the backend already hashes, and it is used for nothing but this
  equality — the model stays the program's to interpret (§6.1).
- **Safe context materialization.** Whatever transport delivered the
  context, the backend MUST materialize it using safe extraction:
  regular files and directories only; absolute paths, `..` after
  normalization, symlinks, hardlinks and device nodes rejected. Writes
  are confined to the layout the context format version defines —
  `context.yaml` at the root, `model/`, `keys/`, and
  `patches/<layer>/` — inside a directory the backend owns.
  `manifest.yaml` is written by the backend at `lock-context` and is
  never an extraction target. Tooling on either side of the contract
  MUST NOT reintroduce extraction of non-regular entries.
- **Resource limits** (CPU, memory, disk quota on `out`, `work` and
  `tmp`) are the backend's to set and to enforce. The program receives
  `limits.jobs` as an authoritative number (§5.2) and MUST behave
  correctly when a limit aborts it — an `out` directory without a
  result document at `result` is a failed invocation by definition.
- **One invocation at a time per `work`, and one session per `work`.**
  The backend MUST NOT run two invocations against the same `work`
  directory concurrently, and MUST NOT hand one `work` directory to two
  sessions. The program cannot check the first, so it is stated as a
  backend duty; a program MAY record its `session` in `work` as a cheap
  guard against the second, and §6.3 specifies exactly what it does when
  that guard fires — including that a foreign marker is a defect on the
  backend's side, not a state the program repairs.

Before every invocation the backend creates the per-invocation
directory, an empty `out`, an empty `tmp`, the session's `work`, the
`events` file if it offers one, and writes the request document
atomically; it write-protects `context` and every non-`writable` tree
with the strongest means its profile has.

### 9.2 What the program MUST NOT do

A recitable list. A conforming program does none of these:

1. Write outside `out`, `work`, `tmp`, `ccache` (if `writable`), the
   `trees` entries marked `writable: true`, and the three files the
   request document names for it: `result`, a temporary file beside it
   (§5.4), and `events`. `context` is not among them (§3.1, §4).
2. Create symlinks, hardlinks, device files, sockets or FIFOs in `out`;
   path segments in `out` are `[A-Za-z0-9._-]+`.
3. Derive any path from `session`, the parent directory of `argv[2]`,
   or a compiled-in absolute path.
4. Use the network.
5. Sign an image, fall back to a default signing key, or run a `build`
   against a context that carries no `keys/signing.pub` (§7.2).
6. Write anything to stdout that is meant to be parsed.
7. Proceed when `patches/<layer>/` names a layer it has no `trees`
   entry for or does not know (§6.2).
8. Block on writing an event, or die when an event write fails.
9. Probe `writable` by attempting a write (§4.1).
10. Apply a patch, write into a `trees` entry, or write into `work`
    during a `verify` (§7.3).
11. Use, delete or overwrite the contents of a `work` directory marked
    for a different session (§6.3).

### 9.3 Egress hardening: what the backend does with `out`

`out` is written by the least trusted component in the system and its
contents travel over the network onto other people's machines. After
the invocation the backend MUST:

- enumerate `out` **without following symlinks** (`lstat` / `O_NOFOLLOW`)
  and reject symlinks, hardlinks (`nlink > 1`), devices, FIFOs and
  sockets;
- normalize every declared artifact path and enforce strict containment
  under the named `root`, skipping any artifact whose `root` it does
  not know (§5.4);
- **re-hash every artifact from the bytes on disk** — declared values
  are advisory, and the comparison is against the one legal spelling of
  §3.3.1, never case-insensitive: a declared hash in any other rendering
  is a mismatch, not a value to fold;
- serve exactly the intersection of declared and verified; files that
  were not declared are not served, but they are not deleted either —
  they are diagnostic material;
- apply size caps during enumeration, from the bytes on disk — an
  artifact entry declares no size.

Attribution always uses the context ID the backend computed itself;
`result.context` exists only to be compared against it.

## 10. ccache

If the request document carries `ccache`:

- **`writable: false`**: the program MUST treat it as a read-only
  secondary cache, with its own primary cache in `work` or `tmp`. Jobs
  benefit from the warm shared cache and cache within their own build
  without ever writing the shared store.
- **`writable: true`**: the program MAY use it as its primary cache.

Shared backends MUST offer a shared cache read-only for untrusted work;
cache warming is a deliberate operator invocation with a writable cache
and trusted contexts only (no user patches). If `ccache` is absent, any
cache is the program's own and dies with the session.

The internal layout of the shared store is backend policy. Recommended:
one subdirectory per implementation, named from `describe`'s
`program.id`, so that two foreign images cannot corrupt each other's
store.

Note a cost the contract does not remove: cache hit rates depend on
stable paths. MCUHome's own container mounts the workspace at its own
host path and sets `CCACHE_BASEDIR` to exactly one prefix
(`mcuhome/compiler/container.py:362`) — a single prefix does not normalize the
independent roots `context`, `trees`, `work` and `tmp`. Keeping those
paths stable per session, or using `-ffile-prefix-map`, is backend
policy, not contract.

## 11. Versioning and evolution

- The invocation argv (§5.1) and the exit codes (§5.3) are frozen for
  contract v1. There are no frozen mount points — §4 replaces them.
- The immortal preambles are frozen: `{request, result}` at the top
  level of every future request format version, `{result, status}` at
  the top level of every future result format version. They are what
  lets an old program refuse a new backend legibly instead of dying
  with a bare non-zero exit.
- New capabilities arrive as new **actions**, announced in
  `program.actions`; an unimplemented action is `unsupported.action`,
  not an exit code.
- New parameters and new top-level fields arrive in the request
  document and are ignored unless named in `required`; an unhonourable
  `required` pointer or value is `unsupported.required` (§5.2).
- The context format evolves via the `context` format version; the
  hashing rule of §3.3 is locked per format version, and the lexical
  form of §3.3.1 is part of that rule rather than a presentation
  detail — the ID is a hash over a text encoding, so a second accepted
  spelling would be a second ID for the same bytes.
- The request document evolves via the `request` version, the result
  document via the `result` version; unknown fields are ignored, unknown
  `status` values read as `failure`.
- The registries — `reason` values, artifact `role` values, event
  names, layer names — are append-only and owned by the MCUHome
  project. Third parties use `x-` prefixes. Unknown values are handled
  as their class and passed through verbatim.
- Two documents this contract points at carry their **own** format
  version and evolve on it, independent of the contract version: the
  build report (`report`, §7.2.1) and the SDK package's metadata (`sdk`,
  §6.1). What this contract fixes about them is where they are found and
  the names of the fields it names itself; everything else is free.
- Deliberately **not** frozen, and therefore free to change: mount
  points and directory names, the internals of an image, the contents of
  `params`, the ccache store layout, reproducibility knobs
  (`SOURCE_DATE_EPOCH`, locale, umask), liveness and timeout policy, and
  the language the program is written in.
- An incompatible change to anything frozen above is a new contract
  version, declared via `org.mcuhome.contract`.

Nothing is deployed against contract v1 yet, and while that holds a
defect in this document is corrected where it stands rather than
versioned around. A new contract version is the answer to a changed
intent; it is not the answer to a sentence that failed to say what was
intended, and spending version 2 on one would leave every future reader
with two documents to reconcile for no gain. That licence ends with the
first implementation that depends on this text.

**Context format 1 does not exist** (E61, product owner, 2026-08-11).
It was the format this document was first written against: the context
pinned a build container by digest, and the digest was one of four
hashed inputs of §3.3. That asked the wrong party for the wrong value —
a client knows which Zephyr its device needs and cannot know which
images a given build server holds, so the pin was either a guess or a
round trip that had to happen before a context could exist at all.
Format 2 replaces it: the context states a Zephyr *line*, the backend
resolves that to a container and records the resolution in
`manifest.yaml`, outside the identity.

The replacement is **not** a migration and this document describes no
version 1. Nothing was published against it, no context written to it
exists outside a superseded test, and a reader that accepted both
formats would carry two hashing rules forever to serve no documents. A
`context` version an implementation does not know remains what §3.2 has
always said it is — `unsupported.context`, not a guess — and a *future*
format 3 would be an addition beside 2 rather than a replacement of it,
because by then the window this paragraph depends on will have closed.
