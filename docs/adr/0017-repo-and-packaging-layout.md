# 0017 — Repository and packaging layout for the remote-build architecture

- Status: accepted
- Date: 2026-08-08

## Context

The remote-build architecture has been finalized with the product owner
(2026-08-08). This ADR and its two companions (ADR 0018 build context,
ADR 0019 session protocol and container contract, dashboard ADR 0012
build-server extraction) record it; this one fixes where the pieces
live and how they are versioned.

Today's state: this repository carries the firmware framework (SDK),
the Python builder package including the `mcuhome` CLI, the builder
container definition (`containers/builder/`) and the golden tests. The
dashboard repository carries the web backend, the frontend **and** the
build server (`buildserver/`), per dashboard ADR 0003. The Python
package is not published anywhere — the dashboard installs it from the
sibling checkout (dashboard ADR 0011).

Two invariants pull on the layout. AGENTS.md already states the hard
lesson: Python codegen and C runtime must version in lockstep — which
is why they share a repository. And the dashboard invariants demand
that the build server be installable on a machine that has no
dashboard, and the CLI usable without any dashboard version.

## Decision

### 1. Four repositories

| Repo | Contents | Depends on |
|---|---|---|
| `mcuhome` | SDK (C components, samples), YAML spec + codegen + **lib** (published as a pip package), west manifest (Zephyr pin), builder container definition + CI, golden tests. One shared version for SDK + lib. | — |
| `cli` | Thin command shell | lib (pip) |
| `dashboard` | Web UI + user key handling (detached signing) | lib (pip) |
| `build-server` | Service orchestrating builder containers | builder contract |

### 2. Repo ≠ package

What a consumer depends on is the **published pip package** (the lib),
never a git checkout. The `mcuhome` repository is where spec, codegen,
lib and SDK live and are tested together; the lib is the artifact it
publishes. Depending on the package does not drag in the repository —
the dashboard container stays free of the toolchain, C sources and
west manifest exactly as before.

The SDK is additionally published as its own CI-built, hash-pinned
source package (the `mcuhome-sdk-<version>` archive a build fetches,
ADR 0018) — same repository, same version, different artifact. Repo,
lib package and SDK package are three names for one release.

### 3. One shared version, and why the layout is this one

The recorded reasoning:

- **Atomic contract changes.** A change to the YAML spec, the code
  generator and the C tables contract lands as **one commit** in one
  repository. There is no window in which spec and SDK disagree, and
  no cross-repo PR dance to keep them aligned.
- **Golden tests stay colocated.** The golden files (ADR 0014) that
  pin codegen output against the C contract run in the same CI, on the
  same commit, as both sides they compare.
- **The version pair collapses to a single version.** SDK and lib
  share one version number, so there is no spec ↔ SDK compatibility
  matrix to maintain, test or explain. "Which lib works with which
  SDK" is not a question that can be asked.

The CLI and the dashboard, by contrast, are thin consumers of the lib:
they declare a version range (dashboard ADR 0011) and follow the lib's
releases. The build server does not consume the lib at all — it
depends only on the **builder contract** (ADR 0019, the normative spec
in `docs/design/builder-container-contract.md`), which is what lets it
orchestrate third-party builder containers too.

## Consequences

- The build server moves out of the dashboard repository into its own
  repo; that half of the decision is recorded where its code lives
  today, in dashboard ADR 0012.
- `cli` is a new repository: the command shell becomes a thin layer
  over the lib. The existing rule that using the CLI must never
  require the dashboard is unchanged — it now falls out of the layout.
- The dashboard's dependency changes from a sibling-checkout install
  to the published pip package; the direction and the version-range
  rule of dashboard ADR 0011 are unchanged.
- Publishing the lib becomes part of this repository's release
  process, alongside the SDK package and the builder container image
  (ADR 0007) — three artifacts, one version, one tag.
- This ADR fixes the **target** layout only. The migration order —
  the cheapest restructuring sequence while the repositories are still
  private — is the merge plan, the next phase after this ADR set.
- Related standing decisions: ADR 0002 (repo split), ADR 0005
  (SemVer), ADR 0007 (builder container), ADR 0014 (golden tables
  contract), ADR 0018, ADR 0019; dashboard ADR 0003, ADR 0011,
  ADR 0012.
