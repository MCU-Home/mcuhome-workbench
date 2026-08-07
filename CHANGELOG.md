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
- Builder front half (`mcuhome/`, phase 2 block A): config-tree discovery,
  YAML load with `!secret`, the three validation stages and the canonical
  device model behind `mcuhome validate <device>`, with a pytest suite in
  `tests_py/` that asserts every rejection message and a golden device model
  for `docs/design/examples/00-bmp180-two-endpoints.yaml`.
- Builder code generation (`mcuhome/generate.py`, phase 2 block B): pipeline
  stage 4 turns the canonical device model into a standalone Zephyr
  application — the Matter and channel tables (`mcuhome_config.c/.h`), the
  devicetree overlay, the Kconfig fragment and a `CMakeLists.txt` — behind
  `mcuhome build <device> [--generate-only]`. Output is deterministic and
  clang-format-clean by construction, and golden-file tested byte for byte.
- `samples/matter-node/src/mcuhome_config.c` is now generator output rather
  than a hand-written stand-in, with a new `mcuhome_config.h` next to it
  (ADR 0014: the sample is the codegen regression fixture). `src/main.c`
  keeps only application glue; the channel and sensor-binding tables moved
  into the generated file, where the device configuration belongs.
- Trap-fix Kconfig blocks (`snippets/matter/`, `snippets/debug-rtt/`) closing
  silent-failure modes found during hardware bring-up (entropy downgrade,
  undersized mbedTLS heap/stacks, RTT control-block re-init).

### Removed

- `samples/matter-node` builds for `nrf7002dk/nrf5340/cpuapp` only. Generated
  tables reference the sensor's devicetree node directly, so the sample no
  longer carries the `DT_NODE_HAS_STATUS_OKAY()` guard that let it build for
  `nrf52840dongle/nrf52840` without a sensor — a generated device
  configuration is never built for a board it was not generated for.

### Fixed

- Removed the insecure prototype entropy source now that the netcore entropy
  service provides a real CSPRNG path.
