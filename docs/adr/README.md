# Architecture Decision Records

Non-trivial design decisions are recorded as numbered ADRs in a
lightweight [MADR](https://adr.github.io/madr/) style: **Context /
Decision / Consequences**, plus a status.

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
to this directory with a `Finalized:` date. Final ADRs are
**immutable**: after finalization only the status line may change
(`superseded by NNNN`). Changing a finalized decision means writing a
new draft that supersedes the old final.

Numbers come from one sequence, assigned at draft creation, and follow
the document for life. A final that consolidates several drafts names
the numbers it absorbs; absorbed numbers are retired, never reused.

Statuses: `draft` (in `draft/`), `accepted`, `deferred`,
`superseded by NNNN`.

Project-wide decisions live in this repository (the flagship repo);
dashboard-specific decisions live in
[mcu-home/dashboard](https://github.com/mcu-home/dashboard).

## Final ADRs

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | superseded by 0021 |
| [0002](0002-split-firmware-and-dashboard-repositories.md) | Split firmware and dashboard into separate repositories | accepted |
| [0003](0003-apache-2.0-license.md) | Apache-2.0 as the single project license | accepted |
| [0004](0004-zephyr-west-t2-manifest-and-module.md) | Zephyr west T2 manifest repo that is also a Zephyr module | accepted |
| [0006](0006-matter-sdk-source.md) | Matter SDK source: upstream CHIP vs. Nordic fork | accepted |
| [0007](0007-containerized-toolchain.md) | Containerized toolchain, minimal host requirements | accepted |
| [0008](0008-zephyr-version-strategy.md) | Zephyr version strategy: track latest stable, not LTS | accepted |
| [0009](0009-matter-explicit-yaml-schema.md) | Matter-explicit YAML schema, aligned with devicetree conventions | accepted |
| [0010](0010-matter-only-coap-deferred.md) | Matter-only integration; CoAP deferred to a maintenance channel | accepted |
| [0011](0011-commissioning-and-radio-coexistence.md) | Commissioning strategy and BLE/Thread radio coexistence | accepted |
| [0014](0014-generated-tables-contract.md) | Generated-tables contract and native composed-node topology | accepted |
| [0017](0017-repo-and-packaging-layout.md) | Repository and packaging layout for the remote-build architecture | accepted; packaging superseded in part by 0020 |
| [0018](0018-build-context-and-manifest.md) | The build context: self-contained, content-addressed, archivable | accepted |
| [0019](0019-session-build-protocol-and-container-contract.md) | Session build protocol and the build-container contract | accepted |
| [0020](0020-package-layout-and-the-asynchronous-library.md) | Package layout and the asynchronous library | accepted |
| [0021](0021-draft-first-adr-lifecycle.md) | Draft-first ADR lifecycle | accepted |

## Draft ADRs

Numbers missing above live here — they are the same sequence.

| ADR | Title |
|---|---|
| [0005](draft/0005-semver-and-conventional-commits.md) | SemVer 0.x with Conventional Commits |
| [0012](draft/0012-device-attestation-strategy.md) | Device attestation (DAC) strategy for user-built devices |
| [0013](draft/0013-binary-blob-policy.md) | Binary blob policy, build profiles, and per-device Zephyr pinning |
| [0015](draft/0015-update-and-partition-architecture.md) | Update and partition architecture per board class |
| [0016](draft/0016-device-onboarding-and-flash-transport.md) | Device onboarding, the MCUHome standard state, and flash transport |
