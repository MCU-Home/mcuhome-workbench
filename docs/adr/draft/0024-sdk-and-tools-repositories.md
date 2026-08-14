# 0024 — The SDK repository and the tools repository (public from here on)

- Status: draft
- Date: 2026-08-14
- Will supersede on finalization: parts of [0017](../0017-repo-and-packaging-layout.md)
  (repo topology), and it retargets details of
  [0004](../0004-zephyr-west-t2-manifest-and-module.md) (the manifest
  repository's name) and [0020](../0020-package-layout-and-the-asynchronous-library.md)
  (where the distributions build from, the version-source mechanics)

## Context

The `mcuhome` repository is two things at once: the west manifest
repo/Zephyr module a firmware build consumes, and the home of three
Python distributions living in a subfolder that shares the repo's
name. The remote-build architecture has already separated these
*operationally* — the workbench consumes the SDK as a resolved,
hash-pinned package (ADR 0018/0019), not as a sibling checkout — but
the repository layout still predates that separation. The product
owner judged the mixture not clean and decided to split it while the
cost is lowest: the repositories are private today, nobody external
consumes `west init -m …/mcuhome`.

The cut cannot follow "Python vs. C": the compiler is delivered
**inside the SDK package** and executed in the build container
(contract §6.1), its output must byte-match the C runtime (the ADR
0014 golden tests are single-repo tests today), and the compiler
imports the model. A tools-side compiler or model would make the
SDK-first release order circular.

## Decision

### 1. Two repositories

| Repo | Contents | Distributions |
|---|---|---|
| `mcuhome-sdk` (new) | West manifest + Zephyr module (the repo a `west.yml` pins), C runtime, `components/`, boards, samples, `patches/`, the build-container definition, the SDK-package build, **`mcuhome.model` + `mcuhome.compiler`**, the golden tests | `mcuhome-model`, `mcuhome-compiler` |
| `mcuhome` (stays the flagship) | `mcuhome.workbench` incl. the project/configuration module (ADR 0022), project-wide ADRs, community files — Python at the repository root | `mcuhome-workbench` |

The PEP 420 namespace `mcuhome.*` and every import path stay exactly
as they are; what moves is repository residency, not package identity.

### 2. Release flow: SDK first, tools follow

A release starts in `mcuhome-sdk`: tag X.Y.0, CI builds the
deterministic SDK package and the `mcuhome-model`/`mcuhome-compiler`
distributions (the package served manually today,
`packages.mcuhome.org` later; where the distributions get published
is an open point below). Then `mcuhome` follows with workbench X.Y.0. By
construction the SDK package and the model exist **before** any
workbench of that version can reference them — the ordering the
product owner set, made non-circular by keeping model+compiler on the
SDK side. Version edges across repositories are `~=X.Y.0` (PEP 440,
same major.minor family), the same rule the CLI uses toward the
workbench (cli ADR 0002) — one principle for every edge, enforced
from v1.0; before that, editable checkouts.

### 3. Public from the start — and everything else goes public too

The new repositories are created **public**, and in the same work
block the existing repositories (`mcuhome`, `cli`, `dashboard`,
`build-server`) flip public. The project is open source; hiding it
buys nothing, early users and feedback are worth more. Consequences,
deliberate: no deploy keys or other private-access workarounds in our
own CI any more (the cli workflow's `MCUHOME_DEPLOY_KEY` retires); a
**secrets/history audit of every repository precedes the flip**;
branch protection, Discussions and private vulnerability reporting are
set up at that point (they were deferred exactly until going public).

### 4. What the split block must carry (its own survey, not this draft)

ADR ownership migration two — SDK-shaped ADRs (0004, 0006, 0007,
0008, 0013, 0014, 0015, 0016 among them) move to `mcuhome-sdk` the
way the CLI ADRs moved to `cli`; the workspace becomes
`west init -l mcuhome-sdk`; CI, `tests_py/` and the docs split along
the same line; `docs/design/` and the build-container contract get
assigned by the survey. The exact file-level assignment is the split
block's first step, not decided here.

## Consequences

- The repo a user's `west.yml` pins is the SDK and nothing else; the
  repo a service pip-installs from is Python and nothing else. The
  `mcuhome-sdk` repo still contains Python — deliberately, because
  that Python (model, compiler) *is* part of the SDK artifact and
  versions with it.
- The version-coupling overhead between the two repos is accepted,
  explicitly, and is bounded by the `~=X.Y.0` rule plus the SDK-first
  ordering.
- ADR 0017's "Repo, Python packages and SDK package are names for one
  release" narrows to the SDK side; the tools repo has its own
  release of the same major.minor.
- Everything the split touches in frozen finals (0017 topology, 0004
  naming, 0020 mechanics) is superseded properly at finalization —
  ADR 0021's supersession path, not an edit to history.

## Open points

- Timing of PyPI publication of the distributions (repo visibility
  and package publication are separate decisions).
- Whether `mcuhome-sdk` restructures its interior (e.g. a `python/`
  subtree) during the move or keeps today's layout minus the
  workbench.
- The exact fate of `tests_py/` files that exercise workbench and
  compiler together.
