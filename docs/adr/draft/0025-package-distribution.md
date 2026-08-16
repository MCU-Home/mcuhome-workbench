# 0025 — Package sources: layout, signing and mirrors

- Status: draft
- Date: 2026-08-16

Product-owner design rounds of 2026-08-16. Project-wide: it decides the
second tier of the SDK source ladder ADR 0019 §8 named but left unbuilt,
extends the source configuration of ADR 0022, and binds every future
consumer — the workbench's build methods, the build server's SDK store,
and the command line.

## Context

A build resolves the SDK package from a **source list**, searched in a
fixed order: a local directory first, then `packages.mcuhome.org`, then
any other external source (ADR 0019 §8). Only the first tier exists
(E48): one or more local directories holding
`mcuhome-sdk-<version>.tar.zst` next to the static `index.json` that
`scripts/build_sdk_archive.py` writes. Neither the workbench nor the
build server can speak HTTP at all, so "the official source" is today a
directory somebody filled by hand.

Three properties of what already exists constrain everything below.

**A published version is immutable and eternal.** The context ID hashes
`mcuhome.package.sha256` (ADR 0018 §6), so the same version resolving to
different bytes in two places is a different build claiming the same
identity — which is why the build server treats it as a typed refusal
rather than a fallback. The same reasoning forbids deletion: a pinned
build must stay buildable.

**The index carries no URL, on purpose.** A backend resolves
`(name, version, sha256)` against its *configured* sources; the
context's `package.url` is a hint nobody follows, because a
client-supplied URL that a build server fetched would be server-side
request forgery with a build server's network position behind it. Adding
a host does not change that rule — it adds a source to the operator's
list, nothing more.

**The user configures sources, not hosts.** `sdk_sources` today holds
directories, and a directory is self-contained: an index plus the files
it names. Whatever replaces it over HTTP has to stay self-contained in
exactly the same way, because a source that claimed authority over paths
*above* the one the user pointed at would be a source deciding its own
scope.

On top of that comes a product-owner constraint, stated as a release
gate: **no v1.0 is ever published whose client does not verify
signatures**, and key rotation, key loss and key compromise are designed
in from the start rather than retrofitted. Retrofitting a trust model is
the one thing that cannot be done, because the clients already in the
field are the part that must do the checking.

Second product-owner requirement, of equal weight: **third-party
package sources are explicitly supported** — private and company-
internal registries, not only mirrors of ours. So "is this source
signed, and against which key" cannot be a property of the client. It is
a property of each configured source.

Scope: this ADR decides the whole model. The *implementation* is staged
— the host, its layout, its key material and its publication workflows
first; the fetching and verifying clients (workbench, build server,
command line) as a separate piece of work, against the reference
implementation and test vectors this one produces.

## Decision

### 1. A source is one self-contained registry at one URL

The unit of everything — configuration, trust, publication, mirroring —
is a **source**: one directory, reachable as a local path or as an HTTP
base URL, containing everything needed to use it and nothing about
anything else.

```
https://packages.mcuhome.org/sdk/        ← this whole directory is one source
├── keys.json          keys.json.sig     the source's own trust root (§5)
├── keys/<issued>.json …                 every superseded key set, kept forever
├── mirrors.json       mirrors.json.sig  where this source's data may be fetched (§7)
├── index.json         index.json.sig    the package index (§2)
├── index-<issued>.json …                superseded index parts, kept forever
├── mcuhome-sdk-2.4.0.tar.zst
└── mcuhome-sdk-2.4.0.tar.zst.sha256
```

`packages.mcuhome.org` is a **host that serves sources side by side** —
`/sdk/` today, a community registry for device configurations,
components and third-party board support later. It is not itself a
source and holds no shared trust document: a key that signs in `/sdk/`
has no authority in any other directory, and that separation comes from
the structure rather than from a field anyone has to check.

Deliberately not served here: container images (an OCI registry, GHCR),
Python distributions (PyPI), OTA images.

Consequences of self-containment, all load-bearing:

- **A source is exactly what a local source directory already is.**
  `https://…/sdk/` and `/home/str/mcuhome-sdk-packages/` differ in
  transport and in whether they are signed, in nothing else. One code
  path, and `scripts/build_sdk_archive.py` keeps producing a valid
  source.
- **Mirroring is copying a directory** — `rsync`, `wget -r`, a bucket
  sync. There is no per-mirror path rewriting, because the source
  layout below the base URL is identical everywhere.
- **A third-party registry needs no concept it does not have.** It
  serves one thing; it has one index, its own keys, and lists itself as
  its own mirror.
- **Nothing walks upwards.** Every document a source needs lives at or
  below its base URL.

**Versions are appended, never replaced and never removed** — not an
index entry, not a package, not a superseded key set. (A superseded
*part file* is a container rather than content, and may be pruned once
nothing references it — §2.)

**Pre-releases are not a separate channel.** `2.5.0.dev0` satisfies only
a pre-release constraint, by the PEP 440 rule E52 already fixed. A
`dev/` directory would be a second mechanism for a decided question.

### 2. The index, and how it stays small

`index.json` keeps the shape it has today — `{"packages": {<name>:
{<version>: {file, sha256, size}}}}`, `file` relative to the source — and
gains the common header of §3 plus one optional field:

```jsonc
{ "version": 1, "min_client": 1, "issued": "…", "expires": "…",
  "parts": [ { "file": "index-2026-05-02T081500Z.json",
               "sha256": "…",
               "covers": { "versions": { "min": "1.0.0", "max": "1.9.3" } } } ],
  "packages": { "mcuhome-sdk": { "2.4.0": { "file": "…", "sha256": "…", "size": 512345 } } } }
```

An index that only grows is eventually a document every client downloads
in full to read five relevant lines. **`parts` is a flat list of further
index files, each named with its hash**, so the signature on the head
vouches for all of them through the hashes, and a client reaches any
part in one hop.

- A part file holds `{"packages": {…}}` and nothing else: no header, no
  signature of its own. It is verified by the hash in the head.
- **`covers` says where an entry lives, not merely where it might be.**
  Splitting an index is a publishing decision: the publisher declares
  what a part covers and writes each new entry into the part that covers
  it, or into the head when none does. Rotation by age — "move the
  oldest N when the head fills" — was considered and rejected, because
  it assumes publication is ordered and neither case is: a 2.4.1 patch
  release can follow 3.0.0, and a registry gains names in no order at
  all.
- A client fetches a part only when its `covers` may contain what is
  being resolved; a part without `covers` must always be fetched. For a
  package index the selector is a version range; a name range
  (`"names": {"min": "a", "max": "f"}`) is how a registry of thousands
  of packages shards, and it is the same mechanism and the same client
  code.
- **Part files are immutable and named after their content**, e.g.
  `index-1.x-<first 16 hex of sha256>.json` — a readable shard name for
  humans, content identity for everything else. Adding an entry to a
  part does not change that file: it produces a new one, and the head is
  republished pointing at it. This is deliberately a guarantee **the
  service gives**, not a discipline every client must keep: no cache at
  any layer (client, proxy, CDN, mirror) can then serve stale content
  under a current name, and a mirror that has not caught up answers 404
  and is failed over, instead of answering with an older file that fails
  a hash check. Identical content yields an identical name, so
  republishing is idempotent.
- A superseded part file is a **container, not content**: its entries
  live on in its successor. Once no published head references it, it may
  be pruned — after a generous grace period (90 days), so that a lagging
  mirror and an in-flight client are never cut off mid-resolution. Index
  *entries*, packages and key sets are never removed (§1).
- Because parts are **not** individually signed, a key compromise does
  not orphan history: the next head re-attests all of it.

Nothing forces a split. The SDK will plausibly run for years with an
empty `parts` and every entry in the head — 25 entries are about 3 KB.
The mechanism is in the format now because retrofitting pagination into
a signed, mirrored document later is a format break, and format breaks
cost a `min_client` bump and every old client.

**No compressed sibling of the index.** A `.zst` copy would save what
HTTP `Content-Encoding` already saves transparently, and it would create
the one question this design must never have: which bytes are signed,
the compressed or the decompressed ones. If an index ever grew large,
`parts` is the better answer than compression.

### 3. Every signed document carries the same header

```jsonc
{ "version": 1,                          // schema generation of this document
  "min_client": 1,                       // smallest client generation allowed to use it
  "issued":  "2026-08-16T09:12:31Z",     // RFC 3339, UTC, seconds
  "expires": "2026-09-15T00:00:00Z",
  … }                                    // the document's own body
```

**`issued` is the freshness and anti-rollback comparator.** A monotonic
counter was considered and rejected: a timestamp is generated without
reading the document it replaces, and it says how old a document is,
which a counter does not. Its one weakness — a timestamp *can* go
backwards on a skewed clock or under two concurrent publishers, where a
counter structurally cannot — is closed at the writing end:
**publication refuses unless `issued` is strictly greater than that of
the document it replaces**, which the publisher has in hand anyway.
Clients store the highest verified `issued` per document per source and
refuse anything older.

**`expires` bounds a freeze.** A mirror serving a stale but validly
signed document is a real attack — it keeps a known hole open — and a
signature alone cannot detect it. Expiry is the standard answer (apt's
`Valid-Until`), and unlike asking an origin at build time it works
offline and survives the host being down. The cost is that documents
must be re-signed periodically even when nothing is released: a
scheduled workflow, §10.

A client's clock is an input to the expiry check only. A wrong client
clock can cause a refusal; it can never cause a forged document to be
accepted.

**`version` and `min_client` do two different jobs, and one number
cannot do both.** If a client accepted every `version` at or above its
own, raising the number would not stop an old client; if it accepted
only the exact number it knew, every additive field would break every
old client.

- `version` counts schema generations, starting at 1. It is
  informational — bug reports, our own tooling — and no client refuses
  because of it.
- `min_client` is the **only field that can refuse**. Each client
  carries one built-in integer, its *generation*, and refuses a document
  whose `min_client` exceeds it, telling the user to update the tool.

**Compatibility is therefore declared, and it is a rule on us:**
extensions are additive only — new keys may appear, existing keys never
change meaning or type and are never removed — and clients ignore keys
they do not know.

Two consequences worth stating plainly. Development documents carry
`min_client: 1`; **at v1.0 both the documents and the shipped clients go
to 2**, so no pre-v1 client — which may lack a security fix — can use
the released sources. That replaces a separate development/production
flag. And it leaves a permanent emergency lever: a flaw in clients up to
generation *N* is answered by publishing `min_client: N+1`, after which
those clients refuse instead of quietly continuing.

### 4. Signatures: Ed25519, detached, multi-signature

Every signed document `X` has a sibling `X.sig`:

```jsonc
{ "version": 1,
  "signatures": [ { "keyid": "<sha256 of the raw public key, hex>",
                    "sig":   "<base64 Ed25519 signature>" } ] }
```

The signature covers **the exact bytes of `X` as served** — no
canonicalisation, no re-serialisation, nothing to disagree about.
Detached, so `index.json` stays a plain document any tool can read and
§1's "a source is a directory" survives. A list rather than a single
value, because threshold signing (§5) and dual-signing across a key
overlap (§6) both need more than one.

Algorithm: **Ed25519**, verified through `cryptography`, which the
command line already carries via `imgtool` — signature verification adds
no dependency. The alternatives were weighed and rejected:

| Candidate | Why not |
|---|---|
| GPG | keyring complexity, web of trust, poor library situation |
| sigstore / cosign (keyless) | state of the art for CI artifacts, but verification depends on Fulcio/Rekor and on Sigstore's own trust root — a third party on the critical path of a tool that must work offline |
| python-tuf (full TUF) | correct, but four metadata roles and a heavy client dependency for a problem with a single publisher per source |

What we adopt is **TUF's model, not its implementation**: a threshold
root role, scoped online signing roles, expiry and anti-rollback
metadata, and rotation by a document signed with the previously trusted
keys. What we consciously leave out is in §11.

### 5. The source's key set: `keys.json`

```jsonc
{ "version": 1, "min_client": 1, "issued": "…", "expires": "…",
  "previous": "keys/2026-05-02T081500Z.json",   // null for the first
  "roots":      { "threshold": 2,
                  "keys": [ { "keyid": "…", "public": "<base64 raw 32 bytes>",
                              "not_before": "…", "not_after": "…" } ] },
  "publishers": [ { "keyid": "…", "public": "…",
                    "not_before": "…", "not_after": "…" } ],
  "revoked":    [ { "keyid": "…", "at": "…", "mode": "retired",
                    "reason": "routine rotation" } ] }
```

Two roles, and the separation is the whole point:

| Role | Where the private key lives | Signs | Validity |
|---|---|---|---|
| **root** — 3 keys, threshold 2 | offline, physically separated | only `keys.json` | years |
| **publisher** — one per source, rotating | a protected CI environment | `index.json`, `mirrors.json` | 6–12 months |

`keys.json` is signed by at least `threshold` root keys **of the key set
already trusted** — the source's configured anchor, or a `keys.json`
previously accepted for that source.

**Why 3 keys and threshold 2.** With 2-of-2, *losing* one key is fatal:
the threshold can never be reached again. With 1-of-2, *stealing* one
key is fatal: the thief signs a key set of their own. 3-of-which-2
survives both a loss and a theft, and it is the minimum configuration
that does.

**Why publishers are separate from roots.** A publisher key has to be
online, in CI, on every release, so it is the key that will eventually
leak. Separating it makes that a contained, routine event: a new key set
retires it, no client is reinstalled, no root key moves.

**Scoping publisher keys further — one for the index, one for the mirror
list — was considered and rejected.** The gain is real but one-sided:
the index key is the valuable one, because whoever holds it can add an
entry pointing at a package of their own, while a mirror-list key can
only point clients at a host serving stale content or nothing. And it
does not match the deployment — both keys would live in the same CI
secret store, so their compromise is correlated rather than independent.
A second key, a scope field and a scope check in every verification
would buy protection against a case that rarely occurs alone, at the
price of a more complex construction and one more thing to get wrong.
Should that change, §3's additive rule allows the field later without a
break.

Scoping *across* sources needs no field at all: a key listed in one
source's `keys.json` has no standing in another source, because there is
no document above them. That separation is structural, and it is the one
that matters.

Honest about the residual: **a compromised root key is the one case no
document repairs** — the threshold is what makes a single stolen root
harmless, and beyond that only a new client helps.

More likely than key theft is that **whoever can land a workflow in the
publishing repository can sign**. That is answered operationally, not
cryptographically: publisher keys live in a protected environment with a
required human approval, publication is `workflow_dispatch` only, `main`
is protected, and no trigger runs untrusted code with access to the
environment.

**Revocation lives inside `keys.json`, not in a file of its own.** Two
files mean a hostile mirror can serve one and withhold the other, and
the withheld one is exactly the revocation. One document, one `issued`,
one expiry — nothing to hold back separately, and no split-brain.

### 6. Rotation, retirement, compromise

Keys have **validity windows**, so rotation never invalidates the past:
a document signed while its key was valid stays verifiable afterwards.
Across a changeover the index and mirror list are **signed by both** the
outgoing and the incoming publisher key, so old and new clients read the
same file — the standard transition, and what the signature list in §4
exists for.

Revocation has two modes, and the difference is not cosmetic:

- **`retired`** — an ordinary changeover. The key signs nothing new;
  signatures it made while valid remain valid.
- **`compromised`** — the key is in someone else's hands. Every
  signature by that key is invalid, including old ones, because a thief
  can backdate. Clients must reject them outright.

**A client offline across two rotations must still catch up.** Each
`keys.json` names its `previous`, and superseded key sets are kept
forever at a stable path. A client that cannot verify the current
document with the keys it trusts walks the `previous` chain backwards
until it reaches one it can verify, then verifies forward to the
present. No enumeration to guess, no counter to walk. (The chain needs
no hashes: every key set is independently root-signed. Index parts do
need them — §2 — because only the head is signed.)

### 7. `mirrors.json`: per source, and partial by construction

```jsonc
{ "version": 1, "min_client": 1, "issued": "…", "expires": "…",
  "mirrors": [ { "url": "https://packages.mcuhome.org/sdk/", "operator": "MCUHome" },
               { "url": "https://mirror.example.org/mcuhome/sdk/" } ] }
```

Each source carries its own mirror list, and it lists **where this
source's data may be fetched** — nothing about any other source. A
registry with no mirrors lists itself; that is what our own sources do
today.

This is what makes partial mirroring free: an operator who mirrors only
the SDK appears in the SDK source's list and in no other. There is no
`sections` field to keep truthful, and no way for a mirror list to make
a claim about a directory it does not serve.

- `url` is a **base URL ending in `/`**, pointing at the equivalent
  directory on that mirror. The layout below it is identical
  everywhere, so the client builds `base + "index.json"`. Mirror-
  specific paths are not allowed.
- Everything a source contains may be fetched from a mirror, trust
  documents included — a mirror cannot forge them (signed) and cannot
  advance them (`issued` is inside the signature); it can only withhold,
  which §9's "highest verified `issued` wins" turns into a non-event.
- **`https://` only.** The content is signed, but plaintext still leaks
  who builds what and invites downgrade games.
- `mirrors.json` is signed by the source's publisher key, never by the
  roots: adding a mirror is an operational act and must not cost an
  offline ceremony.

**Mirrors keep their own names.** Putting third parties under
`packages.mcuhome.org` was considered and is neither possible nor
desirable: several CNAME records at one name are invalid DNS (RFC
1034/2181 — a CNAME must be the only data at its name), round-robin over
A records offers no health checking and no fast removal, and any host
answering for our name would need a certificate for it, i.e. we would be
handing out certificates or delegating ACME to strangers. A signed list
is what Arch, Debian and CTAN use, and a suspect mirror is dropped in
minutes rather than after a TTL.

### 8. The anchor belongs to the source, not to the client

Whether a source is verified, and against which root keys, is a property
of **that source as the operator configured it** — never of the client.
A client-owned anchor could only ever describe ours, and private and
company-internal registries are a first-class case.

Source configuration itself is not decided here: it belongs to the
workbench and the command line, and it is explicitly out of scope of
this step, which builds the service and not its consumers. What this ADR
fixes is what that configuration must satisfy:

- A source entry names a trust anchor — a root key set: keyids, public
  keys, threshold — or states explicitly that the source is unverified.
- **Fail-closed defaults.** A local directory defaults to unverified (a
  directory the operator listed is material the operator controls — that
  is what listing it means); a remote source has no default and must
  state one, so an unsigned remote source is always a deliberate,
  visible choice. A local directory *may* be signed, by giving it an
  anchor.
- **Unverified means no trust documents at all**: no `keys.json`, no
  `mirrors.json`, `index.json` read directly from the source. A local
  directory and an unsigned remote directory are then the same code
  path, and `scripts/build_sdk_archive.py` output stays usable as-is.
- Existing configuration keeps working: today's plain list of
  directories must remain valid spelling for the unverified local case.
- Our own source is shipped as a **declared default carrying the
  built-in anchor**, together with a snapshot of its `mirrors.json`
  current at release time, so a client whose `packages.mcuhome.org` has
  vanished still works as long as one listed mirror lives. There is no
  magic host name in the code: it is a default value of a configuration
  option (ADR 0022's layers) that the user may override, extend or
  remove like any other.

Illustration only, not a specification — one possible spelling:

```yaml
sdk_sources:
  - url: https://packages.mcuhome.org/sdk/     # declared default, built-in anchor
  - url: https://packages.example.com/sdk/
    verify: keys/example-root.json
  - path: /home/str/mcuhome-sdk-packages/
    verify: false
```

Per-source state is cached at
`$XDG_CACHE_HOME/mcuhome/sources/<slug>-<sha256 of the source URL, 16 hex>/`
— the slug readable for debugging, the hash for identity — holding the
trust documents, the index and its parts, and the highest `issued` seen
for each. Package files are cached content-addressed and shared across
sources at `$XDG_CACHE_HOME/mcuhome/packages/<sha256>`: a version is its
bytes, so the same package fetched from two sources is one file, and a
version whose bytes changed can never collide with the one already
there.

### 9. The client's verification algorithm

Normative, per source. A client that skips a step is not conformant, and
a release that does not implement all of them does not ship (the gate in
Context).

0. **Unverified sources** (`verify: false`) skip to step 5, reading the
   index from the source itself.
1. **Anchor.** The trust anchor is the one named by this source's
   configuration — for our sources, the client's built-in anchor. Never
   a document fetched at run time: a fetched anchor reduces every
   signature to "the host said so".
2. **Key set.** Obtain `keys.json` and its signature from the source or
   any of its mirrors. Accept only if: at least `threshold` valid
   signatures from root keys of the currently trusted set for this
   source, each within its validity window at `issued` and not
   `compromised`; `min_client` ≤ this client's generation; `expires` in
   the future; `issued` newer than the newest `keys.json` already
   accepted for this source. If it cannot be verified against the
   trusted set, walk `previous` (§6). An accepted document replaces the
   trusted set for this source and is cached.
3. **Mirror list.** Same header checks; one valid signature by a
   publisher key of this source, valid at `issued` and not revoked.
4. **Choose a mirror** from the list.
5. **Index head.** Same header checks; `issued` newer than the cached
   head for this source.
6. **Parts.** Fetch a part only if its `covers` may contain what is
   being resolved, and verify its `sha256` against the head before
   reading it. Part files are immutable and content-named (§2), so this
   check is defence in depth rather than the only thing standing between
   a cache and a wrong answer.
7. **Resolve and fetch.** Resolve the PEP 440 constraint against the
   entries as today, fetch the named file, verify size and `sha256`. Any
   mismatch is a typed refusal and the next mirror is tried; it is never
   a fallback to unverified bytes.
8. **Compromised keys** invalidate their signatures regardless of when
   they were made; `retired` keys do not.

### 10. Publication

```
mcuhome-sdk           release.yml   tag → build_sdk_archive.py → archive + .sha256
                                    attached to the GitHub release
packages.mcuhome.org  publish.yml   workflow_dispatch(tag) → fetch the asset →
                                    verify its sha256 → refuse if the version is
                                    already indexed → write the entry into the part
                                    that covers it, or the head → set issued/expires
                                    → sign → commit
                      refresh.yml   weekly → renew issued/expires on every signed
                                    document of every source, re-sign, commit
```

**Publication pulls; it is never pushed.** The host repository fetches
from the SDK repository's public release, so no repository needs write
access to another: no cross-repository token, no app, no long-lived
secret. `workflow_dispatch` rather than a trigger, because releasing is a
deliberate act — the same reason the archive is built from a commit and
cut once.

Expiry windows: **30 days for the index and the mirror list**, renewed
weekly, so three weeks of CI outage are uneventful. **A year for
`keys.json`** — and its renewal is deliberately *not* automatable: it is
root-signed, the root keys are offline, and a refresh path able to
re-sign it would mean they were not. Renewing it is a ceremony with the
offline keys, and the same occasion rotates the publisher key. A
scheduled check reports remaining validity and fails 60 days ahead, which
is the only mechanism that can announce a ceremony nothing automatic can
perform.

The publisher key lives in a repository environment restricted to the
default branch, so no workflow on any other ref can reach it. It
deliberately carries **no required-reviewer gate**: the weekly refresh
must run unattended, and a gate that turned it into a weekly approval
click would either be clicked without reading or be removed. The real
protection is who may push to the branch the environment is bound to,
plus publication being `workflow_dispatch` only.

The host repository also carries, as part of this work rather than the
later client work:

- **`verify.py`** — the normative reference implementation of §9,
  dependency-light and executable.
- **Test vectors** — valid, expired, rolled back, wrong key, revoked key
  (both modes), out-of-scope publisher, tampered index, tampered part,
  dual-signed across an overlap, `min_client` too high. They make the
  client's verification testable before the client exists, which is what
  turns §9 into a transcription rather than a second design.

### 11. Deliberately not done

- **No cross-source snapshot.** Nothing prevents pairing an old index of
  one source with a new index of another. Sources are independent by
  construction, so the pairing is meaningless today; this is where a
  future need would attach.
- **No scoping of publisher keys.** One publisher per source signs both
  its documents; the reasoning, and the condition under which it would
  be revisited, are in §5. No delegated role tree either.
- **No `timestamp.json`.** Per-source `expires` already bounds a freeze,
  and a document with no consumer is what the workspace hygiene rule
  forbids. The slot is recorded here in case freshness in hours is ever
  needed.
- **No compressed index sibling** (§2).
- **No deletion of content, ever** — no package, no index entry, no
  superseded key set. Unreferenced superseded *part files* are the one
  exception, and only after §2's grace period.

## Consequences

- The SDK ladder's second tier becomes real, and `sdk.unavailable`
  becomes retryable on the build server once its fetcher exists — today
  it is deliberately final, because a source list with no network in it
  cannot promise a retry.
- Source configuration will have to grow from a list of paths to a list
  of entries carrying an anchor (ADR 0022's option registry). That work
  is **not** part of this step; §8 states only the requirements it must
  meet, and today's plain list of directories must keep working.
- Signature verification is a release gate: the client work (fetcher,
  cache, offline switch, build-server tier 2) may be staged after this,
  but none of it may reach v1.0 unverified.
- The command line gains a built-in anchor, a built-in source entry with
  a mirror snapshot, and a client generation number.
- Private and company-internal registries are supported by the same
  mechanism as ours, with their own roots — nothing about a source is
  privileged by being ours.
- Moving our host later — object storage, a CDN — is a change to a
  mirror list and a default, not a format change. No document names a
  host except the mirror list, whose whole job that is.
- Three root keys per source must exist and be kept apart. Until v1.0
  they are development keys, generated without passphrases and kept in
  the workspace; at v1.0 they are regenerated properly and the published
  documents are re-signed, so the history stays verifiable instead of
  being orphaned.
- Publisher keys live in CI and will eventually leak; scoping and the
  root/publisher split make that routine rather than an incident.
