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
- Builder build orchestration (`mcuhome/workspace.py`, phase 2 block C):
  `mcuhome build <device>` now compiles what it generates. It locates the
  west workspace, arranges the environment CHIP's codegen needs
  (`scripts/pyshim` on `PYTHONPATH`, `ZEPHYR_BASE`), refuses by name when
  `gn` or `zap` is missing, runs `west build` with the board and snippets
  from the device model, and reports the image path plus the flash/RAM
  footprint. `-S/--snippet` adds a snippet (e.g. `debug-rtt`) on top of
  the ones the configuration requires; `--no-native` selects the ADR 0007
  builder container and refuses, which is block D.
- The generated application is now buildable: `mcuhome/generate.py` emits
  the Matter/CHIP/ZAP CMake glue (CHIP module registration, data-model
  configuration from the framework `mcuhome-root.zap`) and the
  `CHIPProjectConfig.h` wrapper CHIP resolves app-relative. The same glue
  is carried byte-identically by `samples/matter-node/CMakeLists.txt`, and
  the test suite asserts the two never diverge.
- Board-scoped devicetree moved into the builder's board registry
  (`BoardDef.overlay`), next to the board-scoped Kconfig that was already
  there: a generated nRF5340 application now carries the netcore entropy
  redirect it cannot boot Matter without.
- `samples/matter-node/src/mcuhome_config.c` is now generator output rather
  than a hand-written stand-in, with a new `mcuhome_config.h` next to it
  (ADR 0014: the sample is the codegen regression fixture). `src/main.c`
  keeps only application glue; the channel and sensor-binding tables moved
  into the generated file, where the device configuration belongs.
- Trap-fix Kconfig blocks (`snippets/matter/`, `snippets/debug-rtt/`) closing
  silent-failure modes found during hardware bring-up (entropy downgrade,
  undersized mbedTLS heap/stacks, RTT control-block re-init).
- The builder image (`containers/builder/`, phase 2 block D): ADR 0007's
  single build environment, carrying the Zephyr SDK (`arm-zephyr-eabi`
  only), CMake/Ninja/dtc, west and Zephyr's Python requirements, `gn`,
  `zap` and ccache — every version pinned and every download
  checksum-verified. Tagged in lockstep with the Zephyr pin
  (`ghcr.io/mcu-home/builder:zephyr-<version>-r<revision>`, never
  `latest`), with `mcuhome/container.py` as the single source of truth
  for the tag and `tests_py/` asserting it still matches `west.yml`.
- Builds run in that image by default (`mcuhome/container.py`): the
  workspace is bind-mounted at its own absolute path, the container runs
  as the calling user so nothing is left behind owned by root, and the
  ccache is a host directory that outlives it. `--image` and
  `MCUHOME_BUILDER_IMAGE` select another image; a missing docker, an
  unreachable daemon and a missing image are three separate refusals with
  three separate fixes.
- ccache now covers the Matter half of the build as well: the generated
  application (and `samples/matter-node/CMakeLists.txt`, byte-identically)
  hands Pigweed's `pw_command_launcher` GN argument into CHIP's inner GN
  build, which Zephyr's own ccache wiring never reaches. Zephyr's
  `USE_CCACHE=0` switches off both halves.
- CI now runs the twister suites (`tests/`, `native_sim/native/64`) inside
  the builder image, with the `matter` west group excluded because every
  suite is CHIP-free by design, and publishes the image to ghcr.io when
  its tag does not exist yet. This closes the workflow's `TODO(twister)`
  block.

- Commissioning-code support in the builder (`mcuhome/pairing.py`,
  `mcuhome/provision.py`, phase 3 block 4). `network.matter:` gains
  `discriminator:`, `passcode:` and `salt:`, and `mcuhome init-pairing
  <device>` draws them from the system CSPRNG and edits them into the
  device's own configuration — line by line, so comments and indentation
  survive — or into `secrets.yaml` with `!secret` references
  (`--secrets`). Randomness therefore happens once per device rather than
  once per build, which is what lets per-device credentials coexist with
  byte-identical rebuilds. `--force` replaces existing credentials and
  says what that costs. A Matter device with no credentials is a
  validation error; `use_test_pairing: true` selects the tuple published
  with the Matter SDK, verbatim, for bench devices.
- The builder computes the SPAKE2+ verifier itself, in pure Python
  (PBKDF2-HMAC-SHA256 plus one P-256 scalar multiplication, ~30 lines, no
  new dependency), and emits the whole commissioning identity —
  vendor/product ID, discriminator, passcode, iteration count, salt and
  verifier — as one indivisible Kconfig group from one function. CHIP
  checks none of those symbols against each other on Zephyr, so a
  passcode written without its verifier used to yield firmware that
  builds, boots, advertises itself and then refuses every commissioner.
  PBKDF2 iterations default to 10000 (CHIP: 1000); the device stores the
  finished verifier and never runs PBKDF2, so the cost is the
  commissioner's alone.
- `mcuhome validate`, `build` and `init-pairing` print the device's manual
  pairing code and `MT:` QR payload. Printed only: the builder keeps no
  record of a device's codes beyond the configuration the user owns.
  Verifier, QR payload and manual code are golden-tested against the
  vectors in the pinned connectedhomeip checkout, including the two codes
  the hardware-verified nRF7002-DK was commissioned with.
- MCUboot and zcbor in the west manifest at Zephyr 4.4.0's pinned
  revisions, and a `boot-mode` snippet supplying the GPREGRET retention
  node for buttonless reboot-into-recovery on the nRF7002-DK and the
  nRF52840 dongle.
- Update and partition architecture fixed per board class (ADR 0015):
  MCUboot on every target via sysbuild, swap with the secondary slot in
  external flash where a second part exists, single slot plus CDC-ACM
  serial recovery on 1 MiB-internal boards, the scheme and partition table
  as per-board registry data, one real signing key per user, and the
  SemVer to Matter `SoftwareVersion` mapping. Sized from measured images,
  not estimates.
- Device onboarding fixed (ADR 0016): no SWD is the design assumption, one
  board-specific bootstrap step brings any board into the same "MCUHome
  standard state" by replacing the vendor bootloader through the vendor's
  own update mechanism, and browser flashing goes over Web Serial with an
  SMP client MCUHome writes itself.

- The per-board update scheme and flash layout in the builder's board
  registry (`BoardDef.update_scheme`, ADR 0015 decision 2): which MCUboot
  mode a board uses, where staging lives, how recovery is entered, the
  partition table that follows, and the bootloader Kconfig and snippets
  that go with it. The nRF7002-DK carries class A — swap with the
  secondary slot on its MX25R64 over SPI4, 64 KiB boot / 928 KiB slot0 /
  32 KiB storage internally — and `storage_partition` keeps the address
  and size the board's own devicetree already gives it, so a device that
  is re-flashed into this layout keeps its Matter fabric. Nothing in the
  builder branches on a board name; `tests_py/test_registry.py` asserts
  that by reading the source of every other module.
- Firmware signing (`mcuhome/signing.py`, ADR 0015 decision 8): one real
  ECDSA P-256 key per user, generated on first need into
  `$XDG_CONFIG_HOME/mcuhome/signing.key` with owner-only permissions,
  outside every repository and every build directory. `--signing-key` and
  `MCUHOME_SIGNING_KEY` point elsewhere — that is the future dashboard's
  path. The key pair is one P-256 scalar multiplication and a PKCS#8
  encoder (`mcuhome/p256.py`, shared with the SPAKE2+ verifier), so the
  builder still has one runtime dependency; the output is byte-shaped
  like `imgtool keygen -t ecdsa-p256` and either can replace the other.
  **MCUboot's demo key is never used** — its private half is published,
  so signing with it only looks like a signature.

### Changed

- `mcuhome build` builds two images, not one (ADR 0015 decision 1): stage
  5 moved to `west build --sysbuild`, so every device now boots through
  MCUboot and its application is signed and linked into `slot0`. Stage 4
  emits the sysbuild half of the tree — `sysbuild.conf`, and
  `sysbuild/mcuboot.{conf,overlay}` — from the board's update scheme, and
  the build summary reports both images with their footprints, the
  combined hex and the flash layout they were built against. Snippets are
  named per image (`-Dapp_SNIPPET=…`, `-Dmcuboot_SNIPPET=…`): sysbuild
  hands a bare `-S` to every image, and `-S matter` in a bootloader is an
  assignment to symbols MCUboot has never heard of. A build directory from
  before this change is rebuilt pristine automatically, because CMake
  would otherwise refuse it with a message about source directories.
  Measured on the nRF7002-DK: MCUboot 63.1 KiB of its 64 KiB partition,
  the application 551.7 KiB of 923.9 KiB.
- A build also produces `merged_<board target>.hex`, every image at its
  own offset in one file — what bringing a board into the MCUHome
  standard state writes, and on a development kit one flash of one file.
- Stage 4 leaves a generated file alone when its content is already
  right, mtime included. CMake watches the application's files, so
  rewriting an unchanged `CMakeLists.txt` reconfigured every image and
  re-ran the Matter sub-build — the difference between a two-minute and a
  twenty-minute rebuild after a one-line change.
- Build parallelism is auto-detected instead of a static `-j2`:
  `mcuhome.workspace.auto_jobs` computes
  `min(cpu_count, max(2, available_ram_gb // 2))` from live CPU count and
  `/proc/meminfo` `MemAvailable` (`mcuhome.workspace.detect_jobs`),
  budgeting ~2 GiB per job — measured CHIP C++ compiles peak around
  1-1.5 GiB per job, and links, though they spike higher, are serialized.
  `mcuhome build --jobs N` overrides it; failing that, `MCUHOME_JOBS=N`
  does; failing that, auto-detection runs
  (`mcuhome.workspace.resolve_jobs`, the single resolution point — the
  build summary states which of the three decided). Resolved once on the
  host before a container build starts docker, since the container's RAM
  budget is the host's (or the WSL VM's), not one guessed at from inside
  a possibly cgroup-limited container. `MCUHOME_CHIP_JOBS`, which caps
  the vendored CHIP GN sub-build's own inner `ninja`, now carries this
  resolved value instead of the previous fixed `2` — as does
  `CMAKE_BUILD_PARALLEL_LEVEL`, which is what reaches each sysbuild
  image's own `cmake --build` (the outer `-o=-jN` does not).
- `mcuhome build` compiles in the builder container by default; `--native`
  is the escape hatch (ADR 0007). Host prerequisites for a compiling build
  shrink to git and docker — the Zephyr SDK, `gn` and `zap` are only
  needed on the `--native` path.
- Generated Kconfig fragments now state the commissioning identity
  explicitly instead of inheriting CHIP's Kconfig defaults. For
  `docs/design/examples/00-bmp180-two-endpoints.yaml`, which asks for
  `use_test_pairing: true`, the emitted values are CHIP's defaults to the
  byte, so the hardware-verified reference image is unchanged.

- `app/` is no longer a placeholder application: `app/src/main.c` is the
  generic MCUHome application main that every generated device shares, and
  `app/CMakeLists.txt` refuses at configure time with a message naming the
  builder. `west build -p -b native_sim mcuhome/app`, previously
  documented as the workspace smoke test, is therefore gone — build a
  sample, or a generated device, instead.

### Removed

- `app/prj.conf` and `app/VERSION`, which only the removed placeholder
  application used.
- `samples/matter-node` builds for `nrf7002dk/nrf5340/cpuapp` only. Generated
  tables reference the sensor's devicetree node directly, so the sample no
  longer carries the `DT_NODE_HAS_STATUS_OKAY()` guard that let it build for
  `nrf52840dongle/nrf52840` without a sensor — a generated device
  configuration is never built for a board it was not generated for.

### Fixed

- Removed the insecure prototype entropy source now that the netcore entropy
  service provides a real CSPRNG path.
- The vendored CHIP GN sub-build always ran its inner `ninja` unparallelized
  from the outer job cap (`config/common/cmake/chip_gn.cmake`, upstream
  `ExternalProject_Add` calls a bare `ninja`), so it used nproc+2 regardless
  of `-o=-j2` — an OOM risk on RAM-constrained targets such as the
  Home-Assistant-add-on class hardware in ADR 0007. New patch hunk in
  `patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch` honors
  `MCUHOME_CHIP_JOBS` from the environment; `mcuhome/workspace.py` and
  `mcuhome/container.py` now set it alongside `-o=-j{JOBS}`, both paths
  reading the same constant.
