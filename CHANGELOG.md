# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
(0.x during incubation; see [ADR 0005](docs/adr/0005-semver-and-conventional-commits.md)).

## [Unreleased]

### Added

- Initial project scaffold: west T2 manifest, Zephyr module definition,
  placeholder application, Python builder package skeleton, community health
  files and architecture decision records.
- Generated-tables contract runtime (ADR 0014): plain-C `mcuhome_node_config`
  table format in `include/mcuhome/matter_tables.h`, with the framework
  (`components/matter/`) owning all translation to CHIP/ember structures.
- Native composed-node topology (ADR 0014): framework-owned `mcuhome-root.zap`
  (endpoint 0 only), dynamic endpoints registered directly under root — no
  aggregator/bridge.
- Typed channel layer (`include/mcuhome/channel.h`), a generic sensor poller
  component (`components/sensor/`), and the BMP180 two-endpoint
  `samples/matter-node` sample, commissioned end to end into a production
  Home Assistant instance over Thread.
- Network-core entropy service and CTR-DRBG driver (`drivers/entropy/`,
  `samples/netcore-radio/`), seeding CHIP's PSA crypto over an `ipc0`
  endpoint on the nRF5340 network core.
- CHIP-free table validator (`components/matter/src/table_validate.c`) and
  three native_sim test suites (`tests/matter_tables/`, `tests/channel/`,
  `tests/entropy_ipc/`).
- First CI workflow (`.github/workflows/ci.yml`): lint and licensing checks
  (ruff, REUSE), landed together with the first test suite.
- Trap-fix Kconfig blocks (`snippets/matter/`, `snippets/debug-rtt/`) closing
  silent-failure modes found during hardware bring-up (entropy downgrade,
  undersized mbedTLS heap/stacks, RTT control-block re-init).

### Fixed

- Removed the insecure prototype entropy source now that the netcore entropy
  service provides a real CSPRNG path.
