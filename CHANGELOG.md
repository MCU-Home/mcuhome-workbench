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

- **The builder's interface contract** (dashboard ADR 0011 decision 4 —
  "Block 0"): everything a program embedding the builder needs, and every
  one of those things improves the plain CLI as well.
  - `mcuhome.api`, the supported programmatic surface and the only part
    of the package covered by the SemVer promise: `open_config_tree`,
    `find_device`, `load_model`, `validate_device` (which reports *every*
    problem instead of raising on the first), `registry_data`,
    `config_json_schema`, `read_manifest`. Everything else in the package
    is an implementation detail.
  - `ConfigError.to_dict()` and `error_dicts()`: message, file (relative
    to the configuration tree), line, column, dotted key, hint and error
    kind, so a validation error can arrive in an editor's gutter as a
    marker rather than in a log pane as a line of text.
  - `build-manifest.json` in every build directory
    (`mcuhome/manifest.py`, builder-pipeline.md §7): device, versions,
    board, per-image role/path/size/SHA-256, the snippets and job count
    the build ran with, and the `imgtool` parameters the application is
    signed with. Deterministic apart from the sizes and hashes it
    measures; no timestamps, no host paths.
  - `--json` on `validate` and `build`: one machine-readable document on
    stdout instead of the human rendering (the build log moves to
    stderr), errors in the same shape, exit codes unchanged.
  - `mcuhome new <device> --board <target>`: a complete starter
    configuration with a commented, working hardware example. It does not
    draw commissioning credentials — it names `mcuhome init-pairing` as
    the next step, because those are drawn once and a scaffold must be
    repeatable.
  - `mcuhome schema [config|registry]`: a JSON Schema for `main.yaml`
    (editor validation and autocomplete) and the registry as data —
    boards with their update scheme and ADR 0016 bootstrap instructions,
    drivers keyed by `compatible`, clusters, device types, and the
    "planned" tables with their reasons. Both are golden-tested, so
    adding a board changes what a dashboard offers with no dashboard
    release.
- **Detached signing** (ADR 0015 decision 8, the ADR's §8 refinement):
  `mcuhome build --no-sign --public-key <file>` compiles the bootloader
  with the public key compiled into it and leaves the application
  unsigned, so no private key ever has to be on the machine that
  compiles; `mcuhome sign <build-dir>` applies the signature afterwards,
  reading the `imgtool` parameters back out of the build manifest and
  running the same tool with the same arguments Zephyr's own
  `cmake/mcuboot.cmake` would have used. `mcuhome public-key` writes the
  half that may travel. A detached build leaves nothing behind that looks
  flashable and is not: no `zephyr.signed.*`, and no combined hex (which
  sysbuild would fill with the *unsigned* application). Verified on the
  real toolchain: the bootloader built from the public key alone is
  **byte-identical** to the one built from the private key, and an
  inline-signed and a detached-signed application agree in header,
  payload, protected TLVs, image digest and key hash — differing only in
  the ECDSA signature, which is randomized by construction.
- **Firmware over the air** (ADR 0015 decision 5), on board class A and
  gated by the board's update scheme rather than by a Kconfig flip:
  - The **OTA Software Update Requestor cluster (0x002A)** and the
    Provider client (0x0029) are in the framework ZAP
    (`components/matter/zap/mcuhome-root.zap`, still endpoint 0 only), and
    the framework instantiates CHIP's `DefaultOTARequestor`,
    `BDXDownloader` and `DefaultOTARequestorDriver` in a bring-up stage of
    its own. Enabling `CONFIG_CHIP_OTA_REQUESTOR` alone had been measured
    to change the image by *zero bytes* — with no cluster in the ZAP,
    `--gc-sections` dropped everything CHIP compiled.
  - **MCUHome's own image processor**
    (`components/matter/src/ota_image_processor.cpp`). CHIP's Zephyr one
    opens `DT_CHOSEN(zephyr_flash_controller)` and applies the
    `slot1_partition` offsets to it, which on a board whose staging slot
    lives on an external part would write into `slot0` and `storage`
    instead. MCUHome's looks the slot up in the flash map, so it writes to
    whichever part actually holds it, checks the incoming image's vendor
    and product IDs and its size against the slot before writing a byte,
    and applies the update as an MCUboot **test** image — never permanent,
    which is what leaves a way back.
  - A **CHIP-free Matter OTA header parser**
    (`components/matter/src/ota_image_header.c`): static buffers, no heap,
    and exercised on the host by `tests/ota_image_header/` against golden
    headers captured from CHIP's own `ota_image_tool.py` plus hand-built
    malformed ones.
  - The builder emits the **`.ota` file** itself, wrapping the *signed*
    image (`mcuhome/ota.py`), and the build manifest gains an `ota` block.
    Written by MCUHome rather than shelled out to CHIP's tool for one
    reason: an .ota wraps the signed image, signing happens where the key
    is (ADR 0015 decision 8), and that machine has no Matter SDK. The
    result is byte-identical to CHIP's tool's, which the pytest suite
    checks wherever the SDK is present.
- **`device.version`** (ADR 0015 decision 9): a SemVer string in the
  device configuration, defaulting to `0.1.0`, mapped to Matter's
  `SoftwareVersion` as `major << 24 | minor << 16 | patch << 8` with the
  low byte reserved for a tweak counter. One function
  (`mcuhome.ota.kconfig_lines`) emits MCUboot's image version and the two
  CHIP symbols together, for the same reason the commissioning identity is
  emitted as one group: a build in which they disagree updates to an image
  the controller then reports as the wrong version, and nothing warns.
- **Health foundation** (`lib/health/`, ADR 0015 health amendment):
  - Fatal errors **reboot** instead of halting. Vanilla Zephyr halts, and
    `CONFIG_RESET_ON_FATAL_ERROR` is Nordic's symbol, not Zephyr's — so
    this is MCUHome code overriding the weak
    `k_sys_fatal_error_handler()`. A halted node is unreachable and never
    reaches the boot in which MCUboot would revert an unconfirmed image.
  - A **hardware watchdog fed from evidence**: the feeder is a work item
    on the system workqueue (running at all proves that queue schedules),
    and it only feeds while every loop registered through
    `mcuhome_health_liveness_register()` has checked in. The framework
    registers the CHIP event loop. A watchdog fed by a timer proves the
    timer runs, which is never the thing that broke.
  - **Image self-confirmation**: `boot_write_img_confirmed()` about 30
    seconds after the Matter stack reports up, and only when this boot
    really is a pending test image. An image that faults, hangs or resets
    before that never confirms, and MCUboot swaps the previous one back.
    The confirmation CHIP's requestor driver would do immediately during
    bring-up is deliberately deferred to that timer.
- The C10 hunk in `patches/connectedhomeip-v1.5.1.0-vanilla-zephyr.patch`:
  CHIP's Zephyr OTA image processor uses `FIXED_PARTITION_OFFSET`/`_SIZE`,
  which Zephyr 4.4 deprecates and CHIP's `-Werror` GN build turns into a
  hard build failure. Needed even though MCUHome does not use that file —
  CHIP compiles it whenever the requestor is enabled.
- **`mcuhome build --model <device-model.json>`** (builder-pipeline.md §6,
  dashboard ADR 0007 decision 4): build a canonical model that some other
  machine already resolved, starting at stage 4. It reads no configuration
  tree and no secrets file, which is what makes a build server a machine
  that needs no trust. `mcuhome.api.read_model` is the same thing in
  process and refuses a `model_version` it does not implement by naming
  both numbers. The two routes produce byte-identical trees; the source
  configuration's file name became a model field (`device.source`) so that
  stage 4 is a function of the model alone, which the generated "generated
  from" headers had quietly made untrue.
- Build-context creation and the normative context identity
  (`mcuhome/context.py`; ADR 0018, builder-container-contract.md §3):
  `create_context` writes the self-contained input artifact of a remote
  build — `manifest.yaml`, the canonical device model under `model/`,
  patches passed through as ordinary integrity entries — and
  `verify_context` recomputes every file hash and the context ID from the
  bytes actually present, so declared values stay advisory. The ID is the
  SHA-256 over the RFC 8785 canonical JSON of exactly the build-relevant
  fields (SDK package hash, target board, sorted file integrity list);
  the rule is locked with context format version 2 and anchored by a
  golden vector in `tests_py/test_context.py`.
- Context format 2 (E61, product owner, 2026-08-11): a context states
  the **Zephyr line** it needs (`zephyr:`, from the model's
  `toolchain.zephyr_line`, ADR 0013) instead of pinning a build
  container by digest, and the backend resolves that requirement to a
  container and records image, tag and digest in `manifest.yaml`,
  outside the identity. The client cannot know which images a given
  build server holds, so the digest was the wrong party's value; the
  line is one the device configuration already decided. Format 1
  disappears without migration — nothing was published against it —
  which is what makes this a version bump with no compatibility surface.
  `mcuhome build --method local` refuses, before it writes anything,
  when the image on this host carries another line — or no
  `org.mcuhome.zephyr` label at all, since "absence is never read as
  compatible" (§2.1.1) — naming what the image says and what the device
  needs.
- A crash now leaves a breadcrumb the next boot reports
  (`CONFIG_MCUHOME_CRASH_BREADCRUMB`, `lib/health/breadcrumb.c`; ADR 0015
  health amendment). A fatal error reboots, which is the right behaviour
  and also the reason nobody ever learns what happened: the fault dump
  goes out over a transport that on a deployed node has no reader, and
  the reset takes the evidence with it. The handler now writes the reason
  code, PC, LR and the SCB fault registers into `__noinit` RAM — plain
  stores, before anything that can block — and the next boot logs that
  record at `ERR` level and counts it (`mcuhome_health_fault_count()`).
  It carries an integrity word rather than a magic word alone, because
  that RAM holds junk after a power-up and belongs to MCUboot before it
  belongs to the application: losing a report is acceptable, inventing
  one is not. The record's logic sits in `lib/health/breadcrumb_core.h`
  as plain C over a caller-provided struct, so the new
  `tests/health_breadcrumb` suite drives every branch on the host — junk
  patterns, a planted magic word, every single-bit flip of every field.
  About 36 bytes of RAM.
- The hardware watchdog now covers the boot chain, not only the
  application (`CONFIG_MCUHOME_BOOT_WATCHDOG`, `lib/boot_watchdog/`; ADR
  0015 health amendment). A fault inside MCUboot ends in Zephyr's default
  fatal handler, which halts — during a swap that is a device with its
  application half-erased and nothing left in the system able to reset
  it, observed on the bench for over 1.5 hours. The MCUboot image now
  arms the watchdog from a `PRE_KERNEL_2` `SYS_INIT` and takes over
  MCUboot's weak `mcuboot_watchdog_setup()`, so the arming happens once,
  earlier than MCUboot's own arming in `main()`, and with the options
  MCUHome chose; only the pre-kernel window stays unguarded. One timeout
  covers the whole chain because the hardware allows only one — the nRF
  watchdog cannot be reconfigured after it starts, so what the bootloader
  arms is what the application inherits — and `mcuhome.generate` emits
  both ends of it (`CONFIG_BOOT_WATCHDOG_TIMEOUT_MS`,
  `CONFIG_MCUHOME_WATCHDOG_TIMEOUT_S`) from one constant. Costs 1,152 B
  of bootloader flash, most of it the log lines that say whether the
  arming worked.
- **The SDK package is buildable** (`scripts/build_sdk_archive.py`, B2).
  ADR 0019's amendment asks CI to "build and hash the
  `mcuhome-sdk-<version>` archive"; this builds it from a **commit** via
  `git archive` — never the working tree — with the release version read
  out of that same commit, so the file name and the content cannot
  disagree. The bytes are reproducible on purpose, because ADR 0018 §6
  hashes `mcuhome.package.sha256` into the context ID: PAX format,
  entries sorted, one mtime (the commit's committer date), `uid`/`gid` 0
  with empty `uname`/`gname`, modes narrowed to 0755/0644, one zstd
  level. What goes in is an explicit allowlist with a named consumer per
  entry — `patches/`, `packaging/`, `tests_py/`, `tests/`, `docs/` and
  `containers/` are out, and anything new in the repository stays out
  until somebody names it. Alongside the archive go a `sha256sum`-format
  sidecar and a static `index.json` mapping `(name, version)` to file,
  hash and size, with no URL in it: the source list is the operator's
  configuration and `package.url` "is a hint only" nobody fetches. A new
  CI job builds it on `main` and uploads it as a workflow artifact; it
  publishes nowhere. `tests_py/test_sdk_archive.py` proves determinism by
  building twice, pins the allowlist in both directions, and drives the
  build server's own `sdkstore.acquire_sdk` over a real archive in
  process — the one place where the two repositories' assumptions about
  one file meet.
- Builder image **r7** (`ghcr.io/mcu-home/builder:zephyr-4.4.0-r7`):
  `/mcuhome/describe.json`, the optional static self-description of
  contract §2.2.1, generated at image build time by *running* the
  program's own `describe` against the baked workspace record and storing
  the result document unread — so the file cannot say anything the
  program would not. It exists because this image is §6.1's own split:
  the launcher is image content, the program body arrives with the SDK
  mount, and a backend that has not chosen a mount point yet therefore
  has no way to ask — while `trees` in the answer is exactly what tells
  it where the mount goes. The three coupling labels were repaired in the
  same revision: the name is `org.mcuhome.toolchain` again (ADR 0020's
  rename had walked into a label the contract owns), `org.mcuhome.zephyr`
  drops west's leading `v` (`4.4.0`), and `org.mcuhome.toolchain` loses
  the `/` the character class of §2.1.1 does not admit
  (`zephyr-sdk-1.0.1`). None of the three was cosmetic — a constraint is
  evaluated against those values, and a container that does not carry a
  named label does not qualify, so the image satisfied no SDK release's
  constraint at all.

### Changed

- **`--native` is gone; `--method local-dev` is the only spelling** (E62).
  The flag predated the build-method names and was kept as an alias for
  them; it is removed rather than deprecated, because the project is not
  public and no invocation outside these repositories can depend on it.
  `mcuhome build --native` is now an unknown argument. Everything that
  named the flag — help texts, refusal hints, `AGENTS.md`, the READMEs,
  the CI comments — names the method instead.
- **The build methods are part of `mcuhome.workbench.api`** (E64).
  `run_build`, `BuildRequest`, `BuildOutcome`, `resolve_method`, the
  method names (`LOCAL`, `LOCAL_DEV`, `REMOTE`, `METHODS`,
  `DEFAULT_METHOD`) and the typed refusals (`UnknownMethod`,
  `MethodUnavailable`, `RemoteNotConfigured`) are re-exported from the
  supported surface, so an embedder drives a build without reaching past
  it into `mcuhome.workbench.buildmethods`.
- **The Python package is three packages** (ADR 0020 decision 1). `mcuhome/`
  became a PEP 420 namespace directory — no `__init__.py`, no module of its
  own — holding `mcuhome.model` (the shared vocabulary: device model,
  registry, the context and build-manifest formats, the frozen context-ID
  rule, error types; no build machinery and no third-party dependency),
  `mcuhome.workbench` (stages 1-3, context creation, the build methods,
  signing; `mcuhome.workbench.api` is the supported surface) and
  `mcuhome.compiler` (stages 4-5 and the invocation-ABI adapter, which is
  what ships in the SDK package and runs in the build container). The line
  is *where the code has to run*, not what it is about. Published as
  `mcuhome-model`, `mcuhome-workbench` and `mcuhome-compiler` from
  `packaging/`, one project file each, all reading one version out of
  `mcuhome/model/__init__.py` (ADR 0017 §3, ADR 0020 decision 8), so no
  compatibility matrix exists to consult. Imports moved with them:
  `from mcuhome.api import …` is now `from mcuhome.workbench.api import …`,
  and so on for every module. One edge crossed the cut the wrong way and
  moved rather than being tolerated — `sha256_file` left `contextdir` for
  `mcuhome/model/hashes.py`, because both sides of build-container-contract
  §3.3 compute it and the build server carries no workbench.
  Packaging-level proof that the split is real: in a fresh environment,
  `mcuhome-model` alone imports and `import mcuhome.workbench` raises, and
  `mcuhome.workbench.api` imports with `mcuhome-compiler` not installed at
  all — which is ADR 0017 §2's "must not drag in the toolchain" as a pip
  fact rather than an import-graph one.
- The repository root ships no distribution. The plain name `mcuhome` is
  reserved for the command line (ADR 0020 decision 2), and the root
  `pyproject.toml` keeps the shared tool configuration and nothing else;
  a contributor installs `-e ./packaging/model -e ./packaging/workbench
  -e ./packaging/compiler` in one pip invocation.
- Builder image **r6** (`ghcr.io/mcu-home/builder:zephyr-4.4.0-r6`): the
  contract program at `/mcuhome/run` follows the package split and execs
  `mcuhome.compiler.abi`. Nothing else about the image changed — the ABI
  module itself is not image content, it arrives with the SDK mount.
- The RTT log backend runs in OVERWRITE mode instead of DROP, in both
  images (`snippets/debug-rtt/debug-rtt.conf` and the class-A bootloader
  lever in `mcuhome/registry.py`) — a safety setting, not a preference,
  and it supersedes the "drop-on-full" of ADR 0015's RTT amendment. DROP
  was chosen so that a wedged or absent host reader could never stall the
  device, and it does not deliver that: `log_backend_rtt.c`'s
  `data_out_drop_mode()` hands straight over to `data_out_block_mode()`
  as soon as the backend has panicked, and `on_write()` then busy-waits
  on `SEGGER_RTT_HasDataUp()` for `LOG_BACKEND_RTT_RETRY_DELAY_MS` — a
  symbol declared under `if LOG_BACKEND_RTT_MODE_BLOCK`, so a DROP build
  cannot set it and inherits the file's hardcoded 10 ms — while re-arming
  its `host_present` latch on every successful write, so the wait never
  gives up. With the one-byte output buffer DROP mode gets, that is
  ~10 ms per character of a fault dump, inside the fatal path ADR 0015's
  rollback story depends on. `data_out_overwrite_mode()` has no retry
  loop in any mode; the price is that a full buffer loses the oldest
  unread bytes instead of the newest, which is the right way round for a
  crash log. Upstream candidate Z12.
- A fault dump now names a thread and an address instead of only a fault
  class (`snippets/debug-rtt`): `CONFIG_EXTRA_EXCEPTION_INFO` puts the
  callee-saved registers and the exception return address in the report,
  `CONFIG_THREAD_NAME` turns "unknown" into the faulting thread's name,
  and `CONFIG_INIT_STACKS` fills stacks at creation so unused depth stays
  measurable afterwards. They live in the always-on debug snippet rather
  than in an opt-in build because the first fault on a device is the one
  worth understanding, and it is never the one somebody rebuilt for — a
  BusFault during an OTA download on 2026-08-08 produced a dump with none
  of this in it. `CONFIG_LOG_PROCESS_THREAD_STACK_SIZE` rises to 2048 in
  the same snippet: vanilla's 768 B is the minimum Zephyr boots with, not
  a size chosen for formatting Matter and OpenThread lines. The cost is
  larger than it looks and is written next to the symbol that causes it:
  `EXTRA_EXCEPTION_INFO` is what makes `ARCH_STACKWALK` selectable,
  `EXCEPTION_STACK_TRACE` then defaults to `y`, and Zephyr compiles the
  whole image with `-funwind-tables` — 52.5 KiB of `.ARM.exidx` and
  `.ARM.extab` on the reference device, which is 91 % of this snippet's
  whole flash cost. It buys the call stack the 2026-08-08 dump did not
  have, and `CONFIG_EXCEPTION_STACK_TRACE=n` is the documented first
  lever if the slot ever gets tight.
- Service-thread stacks in `snippets/matter/` are raised from their
  vanilla defaults, which nobody sized for Matter-over-Thread while a
  700 KiB BDX download runs into an external flash part
  (`OPENTHREAD_THREAD_STACK_SIZE` 3072 → 6144, `NET_RX_STACK_SIZE`
  1500 → 2048, `NET_TX_STACK_SIZE` 1200 → 2048,
  `SYSTEM_WORKQUEUE_STACK_SIZE` 2560 → 4096, `CHIP_TASK_STACK_SIZE`
  8192 → 10240, and on the nRF7002-DK `IEEE802154_NRF5_RX_STACK_SIZE`
  800 → 1024, `IPC_SERVICE_BACKEND_RPMSG_WQ_STACK_SIZE` 1024 → 2048).
  That load profile first ran on 2026-08-08 and ended in a BusFault whose
  faulting thread could not be named, and an out-of-bounds write inside a
  stack frame is the one corruption class the ARMv8-M stack guard does
  not catch. These are headroom with `CONFIG_INIT_STACKS` on to replace
  them with measurements, not sizing decisions — 9.3 KiB of RAM on a part
  that has 448 KiB.
- The watchdog no longer pauses while the core is halted by a debugger.
  `WDT_OPT_PAUSE_HALTED_BY_DBG` was passed unconditionally to survive
  breakpoints, and that is what kept it from firing on the bench: a debug
  connection need not be deliberate or even present, since a killed probe
  session leaves the core halted with nobody attached and the one
  mechanism meant to bring the node back is the one that is paused. Now
  off by default on both sides
  (`CONFIG_MCUHOME_WATCHDOG_PAUSE_ON_DEBUG`,
  `CONFIG_MCUHOME_BOOT_WATCHDOG_PAUSE_ON_DEBUG`) and a deliberate bench
  override — and because the nRF watchdog configuration is write-locked
  once started, a bench session has to set the bootloader's symbol too:
  setting only the application's changes nothing at all.

- Every generated application states the health guarantee out loud
  (`CONFIG_MCUHOME_HEALTH`, `_RESET_ON_FATAL_ERROR`, `_WATCHDOG`,
  `_CRASH_BREADCRUMB`, `_WATCHDOG_TIMEOUT_S`), and every generated
  bootloader states its watchdog. All of these symbols already default to
  `y`, and that is exactly why the generator writes them: a default is a
  decision any board defconfig, snippet or module Kconfig can reverse
  without saying so, and the first evidence would be a node in the field
  that never came back. The same defect class had already removed the RTT
  log transport from generated applications once.
  `samples/matter-node/prj.conf` states the same four symbols, for the
  same reason.
- Every generated application now carries the `debug-rtt` snippet — the
  RTT log transport — without being asked (debug output is load-bearing
  until v1.0, product-owner directive; see AGENTS.md). Previously the
  bootloader half of every generated build logged over RTT (ADR 0015)
  while the application half was silent unless the user appended
  `-S debug-rtt` by hand. An explicit `-S debug-rtt` stays valid and
  collapses into the built-in one.
- The `mcuhome` command line moved to its own repository,
  [mcu-home/cli](https://github.com/mcu-home/cli), as the thin shell of
  the repo family (mcuhome = SDK + builder library, cli = command
  shell). This package no longer installs a console script; programs
  keep embedding `mcuhome.api`, and the command surface itself is
  unchanged — `pip install mcuhome-cli` (or, while unpublished, the
  sibling checkout) provides the same `mcuhome` command. The CLI
  behavior tests moved with it; `tests_py/` keeps the api-level
  coverage.
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
- Class-A boot partition grown 64 KiB -> 80 KiB on the nRF7002-DK (ADR 0015
  amendment, 2026-08-07), a deliberate headroom choice rather than a size
  requirement: two measured, strictly per-image levers (link-time
  optimization — `CONFIG_LTO=y`, `CONFIG_LTO_SINGLE_THREADED=y`,
  `CONFIG_ISR_TABLES_LOCAL_DECLARATION=y`, -7.55 KiB — and dropping the UART
  driver `MCUBOOT_SERIAL` links in regardless of the selected serial-recovery
  transport, `&uart0 { status = "disabled"; };` in the bootloader-only
  overlay, -1.30 KiB; upstream imprecision, `UPSTREAM-BUGS.md` M2) already
  brought the bootloader to 55.4 KiB, clearing a 15 %-free bar at the
  original 64 KiB on their own. `slot0`/`slot1` shrink together to 912 KiB
  to match (928 previously); `storage_partition` is untouched. The
  application's Kconfig fragment and overlay never see either lever — both
  are scoped to `sysbuild/mcuboot.{conf,overlay}` alone.

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
- OTA staging clears the end of the slot before a download starts
  (`components/matter/src/ota_staging.c`). `flash_img_init_id()` flattens
  the first sector and nothing else, and the progressive erase behind
  `stream_flash` only reaches as far as the image is long — so on a slot
  larger than the image (912 KiB against ~730 KiB on the reference board)
  the swap-status bytes MCUboot keeps at the end of the slot outlive a
  previous, failed attempt and sit there under a fresh "upgrade pending"
  magic, where the interrupted-swap resume logic reads them as its own
  unfinished work. The trailer region is now erased at `PrepareDownload`,
  walking the page layout backwards from the end so that no erase-unit
  size is assumed anywhere in the file, and a failure fails the download
  rather than being swallowed. Two new cases in `tests/ota_staging` cover
  it, including that the image between the two erased regions still reads
  back exactly.
- The CI west-workspace cache now covers `bootloader/`, and its key is
  versioned (`west-2-…`). A cache hit skips `west update` entirely, so a
  project the cache did not carry is simply absent from the workspace:
  the first warm-cache run after the `ota_staging` suite appeared failed
  on a missing `bootutil/bootutil_public.h`, which `flash_img` includes
  via `CONFIG_IMG_MANAGER`. Versioning the key retires caches saved with
  the old path list, which a key derived from `west.yml` alone would
  happily restore.
