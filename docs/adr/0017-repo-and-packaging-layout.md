# 0017 — Repository and packaging layout for the remote-build architecture

- Status: accepted; packaging superseded in part by 0020
- Date: 2026-08-08
- Finalized: 2026-08-14

## Context

The remote-build architecture was finalized with the product owner
(2026-08-08). This ADR and its companions (ADR 0018 build context,
ADR 0019 session protocol and build-container contract, dashboard
ADR 0012 build-server extraction) record it; this one fixes where the
pieces live and how they are versioned.

The state at decision time: this repository carried the firmware
framework (SDK), a single Python builder package including the
`mcuhome` CLI, the build-container definition (`containers/builder/`)
and the golden tests. The dashboard repository carried the web
backend, the frontend **and** the build server, per dashboard
ADR 0003. The Python code was not published anywhere — the dashboard
installed it from the sibling checkout (dashboard ADR 0011).

Two invariants pull on the layout. AGENTS.md already states the hard
lesson: Python codegen and C runtime must version in lockstep — which
is why they share a repository. And the dashboard invariants demand
that the build server be installable on a machine that has no
dashboard, and the CLI usable without any dashboard version.

## Decision

### 1. Four repositories

| Repo | Contents | Depends on |
|---|---|---|
| `mcuhome` | SDK (C components, samples), YAML spec + codegen, the published Python distributions (below), west manifest (Zephyr pin), build-container definition + CI, golden tests. One shared version for everything it publishes. | — |
| `cli` | The command shell — its own decisions live in cli ADR 0002 | `mcuhome-compiler` (pip) |
| `dashboard` | Web UI + user key handling (detached signing) (distribution `mcuhome-ui`) | `mcuhome-workbench` (pip) |
| `build-server` | Service orchestrating build containers (distribution `mcuhome-buildserver`) | `mcuhome-model` (pip) + the build-container contract |

The `mcuhome` repository publishes **three Python distributions over
one PEP 420 namespace** — `mcuhome-model` (the shared vocabulary:
device model, registry, the context format including ADR 0018 §6's
frozen ID rule, error types, version constants; no I/O and dependency-free by
construction), `mcuhome-workbench` (stages 1–3, pin resolution,
context creation, the three build methods, the session-protocol
client, signing) and `mcuhome-compiler` (stages 4–5 plus the
invocation-ABI adapter, shipped inside the SDK package and executed
in the build container).

As originally decided, that row named a single published package —
"the lib", a term that described the extraction history (the remainder
after the CLI split) rather than the thing. One package did not
survive the execution sites the remote-build work fixed: code
generation runs inside the build container out of the mounted SDK
package, the dashboard must never carry a toolchain, and the build
server has one obligation that needs the vocabulary without the
logic. ADR 0020 (2026-08-09) records the split — its decision 1 draws
the line by execution site, not by subject matter — and retires "lib"
as a term; "builder" for the container was retired the same day by
ADR 0019 (read: build container, build-container image,
build-container contract).

The dependency arrows in the table are ADR 0020's. The CLI's one
dependency, `mcuhome-compiler`, pulls `mcuhome-workbench` and
`mcuhome-model` with it — the local-dev case is the one consumer
entitled to all three. The dashboard imports `mcuhome-workbench` in
process and never spawns it (dashboard ADR 0011). The build server
consumes `mcuhome-model` and nothing else (§3). The CLI's own
attributes — thin-shell nature, its distribution bearing the plain
name, versioning — are recorded in cli ADR 0002; ADR 0020 §2 keeps
the renouncing half of the name. The services keep
`mcuhome-ui` and `mcuhome-buildserver`.

### 2. Repo ≠ package

What a consumer depends on is a **published pip package**, never a
git checkout. The `mcuhome` repository is where spec, codegen, the
Python packages and the SDK live and are tested together; the
packages are the artifacts it publishes. Depending on a package does
not drag in the repository — the dashboard container stays free of
the toolchain, C sources and west manifest exactly as before.

The SDK is additionally published as its own CI-built, hash-pinned
source package — the `mcuhome-sdk-<version>.tar.zst` archive a build
fetches (ADR 0018), built deterministically from a commit by
`scripts/build_sdk_archive.py` — same repository, same version,
different artifact. Repo, Python packages and SDK package are names
for one release.

### 3. One shared version, and why the layout is this one

The recorded reasoning:

- **Atomic contract changes.** A change to the YAML spec, the code
  generator and the C tables contract lands as **one commit** in one
  repository. There is no window in which spec and SDK disagree, and
  no cross-repo PR dance to keep them aligned.
- **Golden tests stay colocated.** The golden files (ADR 0014) that
  pin codegen output against the C contract run in the same CI, on the
  same commit, as both sides they compare.
- **The version pair collapses to a single version.** SDK and the
  Python distributions share one version number, so there is no
  spec ↔ SDK compatibility matrix to maintain, test or explain.
  "Which package works with which SDK" is not a question that can be
  asked.

These are arguments about the repository, and they carried unchanged
across ADR 0020's packaging split: the single shared version now
covers three distributions instead of one (ADR 0020 decision 8), and
all three read it from one place — `mcuhome/model/__init__.py` — so
the property holds by construction rather than by discipline.

The CLI and the dashboard, by contrast, are thin consumers: they
declare a version range and follow the releases (the dashboard's rule
in dashboard ADR 0011, the CLI's in cli ADR 0002).
The build server consumes **`mcuhome-model`, and nothing else**
(ADR 0020 decision 4) — no build logic, only the shared vocabulary,
which is what keeps it able to orchestrate third-party build
containers too. This decision originally took the absolute form "the
build server does not consume the lib at all"; the absolute failed on
exactly one obligation: ADR 0019 §8 makes the server recompute the
context ID from received bytes, and ADR 0018 §6 freezes the rule for
that computation and requires both sides of the contract to compute
the same value — so "consumes nothing" would give that one frozen
rule a second implementation in a second repository with no
conformance vectors between the two. The contract dependency stays
alongside the package: the build-container contract (ADR 0019; the
normative spec in `docs/design/build-container-contract.md`) is what
the server orchestrates against.

## Consequences

- The build server moved out of the dashboard repository into its own
  repo; that half of the decision is recorded where its code lived at
  the time, in dashboard ADR 0012.
- `cli` is its own repository. The shell's own decisions — thin-shell
  nature, its single dependency, the rule that using the CLI must
  never require the dashboard — are recorded in cli ADR 0002 and now
  fall out of the layout.
- The dashboard's dependency changes from a sibling-checkout install
  to the published `mcuhome-workbench` package; the direction and the
  version-range rule of dashboard ADR 0011 are unchanged. Interim,
  while the repositories are private and nothing is on an index: the
  supported range lives in `mcuhome/ui/versions.py` and is
  enforced at server startup, the dev setup installs from the sibling
  checkout, and moving the range into the package metadata is release
  tooling for the first published release.
- Publishing the packages is part of this repository's release
  process: **one version, one tag** covers the three Python
  distributions (ADR 0020 decision 8), the `mcuhome-sdk-<version>`
  archive and the build-container image (ADR 0007).
- This ADR fixes the **target** layout only. The migration order —
  the cheapest restructuring sequence while the repositories are
  still private — was the merge plan, the next phase after this ADR
  set; it has since executed, and all four repositories exist in the
  shape of §1.
- Related standing decisions: ADR 0002 (repo split), ADR 0005
  (SemVer), ADR 0007 (build container), ADR 0014 (golden tables
  contract), ADR 0018, ADR 0019, ADR 0020 (the packaging split);
  dashboard ADR 0003, ADR 0011, ADR 0012.
