# Architecture Decision Records

Non-trivial design decisions are recorded here as numbered ADRs in a
lightweight [MADR](https://adr.github.io/madr/) style: **Context /
Decision / Consequences**, plus a status (`proposed`, `accepted`,
`deferred`, `superseded by NNNN`).

Project-wide decisions live in this repository (the flagship repo);
dashboard-specific decisions live in
[mcu-home/dashboard](https://github.com/mcu-home/dashboard).

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | accepted |
| [0002](0002-split-firmware-and-dashboard-repositories.md) | Split firmware and dashboard into separate repositories | accepted |
| [0003](0003-apache-2.0-license.md) | Apache-2.0 as the single project license | accepted |
| [0004](0004-zephyr-west-t2-manifest-and-module.md) | Zephyr west T2 manifest repo that is also a Zephyr module | accepted |
| [0005](0005-semver-and-conventional-commits.md) | SemVer 0.x with Conventional Commits | accepted |
| [0006](0006-matter-sdk-source.md) | Matter SDK source: upstream CHIP vs. Nordic fork | deferred |
| [0007](0007-containerized-toolchain.md) | Containerized toolchain, minimal host requirements | accepted |
