# Architecture Decision Records

Non-trivial design decisions are recorded as numbered ADRs in a
lightweight [MADR](https://adr.github.io/madr/) style: **Context /
Decision / Consequences**, plus a status.

This index covers the ADRs that live **in this repository**
(`mcu-home/mcuhome-workbench`) — project-wide decisions, and the ones about the
workbench (`mcuhome.workbench`) it publishes. SDK-shaped ADRs (the west
manifest/Zephyr module, the C runtime, `mcuhome.model`/`mcuhome.compiler`)
live in the SDK repository,
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk/tree/main/docs/adr).
Numbers are **one project-wide sequence shared by both repositories** —
gaps in the tables below are not missing files, they are ADRs that live
on the other side.

## Lifecycle: draft first, final when real (ADR 0021)

An ADR starts in [`draft/`](draft/) as a **living document**: while the
component it decides about is being built, the decision may change, and
then the draft's *text* changes — no amendment or erratum sections,
ever; git history is the changelog. Drafts may be split, merged, or
deleted. `draft` describes the document's maturity, not missing
approval: the decisions in a draft are product-owner-approved when they
are recorded.

When the component is implemented and verified, the ADR is finalized:
rewritten from the real result — the code is the authority — and moved
to the top-level `docs/adr/` of whichever repository owns it, with a
`Finalized:` date. Final ADRs are **immutable**: after finalization only
the status line may change (`superseded by NNNN`). Changing a finalized
decision means writing a new draft that supersedes the old final.

Numbers come from one sequence, assigned at draft creation, and follow
the document for life, across repositories. A final that consolidates
several drafts names the numbers it absorbs; absorbed numbers are
retired, never reused.

Statuses: `draft` (in `draft/`), `accepted`, `deferred`,
`superseded by NNNN`.

Project-wide decisions live in this repository (the flagship repo);
dashboard-specific decisions live in
[mcu-home/mcuhome-ui](https://github.com/mcu-home/mcuhome-ui).

## Final ADRs (this repo)

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | superseded by 0021 |
| [0002](0002-split-firmware-and-dashboard-repositories.md) | Split firmware and dashboard into separate repositories | accepted |
| [0003](0003-apache-2.0-license.md) | Apache-2.0 as the single project license | accepted |
| [0017](0017-repo-and-packaging-layout.md) | Repository and packaging layout for the remote-build architecture | accepted; packaging superseded in part by 0020 |
| [0018](0018-build-context-and-manifest.md) | The build context: self-contained, content-addressed, archivable | accepted |
| [0019](0019-session-build-protocol-and-container-contract.md) | Session build protocol and the build-container contract | accepted |
| [0020](0020-package-layout-and-the-asynchronous-library.md) | Package layout and the asynchronous library | accepted |
| [0021](0021-draft-first-adr-lifecycle.md) | Draft-first ADR lifecycle | accepted |

## Draft ADRs (this repo)

| ADR | Title |
|---|---|
| [0005](draft/0005-semver-and-conventional-commits.md) | SemVer 0.x with Conventional Commits |
| [0022](draft/0022-project-and-configuration-model.md) | The project directory and the configuration model |
| [0023](draft/0023-builder-configuration.md) | Builder configuration |
| [0024](draft/0024-sdk-and-tools-repositories.md) | The SDK repository and the tools repository (public from here on) |
| [0025](draft/0025-package-distribution.md) | Package distribution: host layout, signing and mirrors |
| [0026](draft/0026-container-paths-and-the-compiler-cache.md) | Where a build sits in its container, and where its cache lives |
| [0027](draft/0027-seat-tokens-for-a-busy-build-server.md) | Seat tokens: how a busy build server hands out turns |
| [0028](draft/0028-one-naming-scheme-for-repositories-and-distributions.md) | One naming scheme for repositories, distributions and import paths |

## Elsewhere in the sequence

Numbers 0004, 0006-0011, 0014 (final) and 0012, 0013, 0015, 0016 (draft)
are SDK-shaped ADRs (the west manifest/Zephyr module, Matter SDK
selection, the containerized toolchain, the generated-tables contract,
device attestation, binary blobs, updates/partitioning, onboarding) and
live in
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk/tree/main/docs/adr)
— both its finalized top level and its `draft/`.
