# AGENTS.md — MCUHome firmware framework

Guide for AI coding agents (and new human contributors) working in this
repository.

## What this project is

MCUHome is an open-source alternative to ESPHome: users describe a smart
home device in YAML, the builder produces Zephyr-based firmware. Networking
uses CoAP and Matter (no custom API protocol); transports are WiFi and
Thread, including Sleepy End Devices (SED). The web interface lives in a
separate repository ([mcu-home/dashboard](https://github.com/mcu-home/dashboard)).

**Current phase.** Phase 1 (firmware runtime: tables-contract framework,
channel layer, netcore entropy, BMP180 two-endpoint sample) is complete
and hardware-verified against a production Home Assistant over Thread.
Phase 2 (the Python YAML builder) is in progress: `mcuhome validate`
runs the front half of the pipeline (load → validate → resolve into the
canonical device model), and `mcuhome build` goes all the way to a
flashable image — stage 4 generates the per-device Zephyr application
(tables, overlay, Kconfig fragment, CMakeLists) and stage 5 compiles it
with `west build` inside the builder image (ADR 0007;
`containers/builder/`), or on the host with `--method local-dev`. Note the
consequence of ADR 0014:
`samples/matter-node/src/mcuhome_config.{c,h}` **is generator output**
and the pytest suite compares it byte for byte — never hand-edit it,
regenerate it. Check `docs/adr/` — immutable finals at the top level,
living drafts in `draft/` (ADR 0021) — before assuming any design
decision.

## Repository map

| Path | Role |
|---|---|
| `west.yml` | West manifest (T2 topology) — Zephyr + modules, pinned revisions |
| `mcuhome-sdk.json`, `bin/generate` | The SDK package's §6.1 interface: the metadata file names `bin/generate` as the code-generation entry point a build container invokes as a child process (body: `mcuhome/compiler/sdkentry.py`) |
| `zephyr/module.yml` | Makes this repo consumable as a Zephyr module |
| `CMakeLists.txt`, `Kconfig` | Zephyr module build entry points |
| `mcuhome/` | Python source tree — a PEP 420 **namespace** with one subpackage per distribution (ADR 0020): `model/` the shared vocabulary, `workbench/` stages 1-3 plus the build methods and signing, `compiler/` stages 4-5 plus the invocation-ABI adapter. No `__init__.py` at this level and no module directly under it. `mcuhome.workbench.api` is the supported surface. The `mcuhome` command line is a thin shell in its own repo ([mcu-home/cli](https://github.com/mcu-home/cli)) |
| `packaging/` | One project file per published distribution — `mcuhome-model`, `mcuhome-workbench`, `mcuhome-compiler` — each shipping exactly its subpackage out of the shared tree above. The version is read from `mcuhome/model/__init__.py`, so all three carry one number (ADR 0020 decision 8) |
| `components/` | Components: Python schema + C sources side by side |
| `app/` | The generic application main every generated device shares — **not** a buildable app |
| `boards/`, `drivers/`, `dts/bindings/` | Out-of-tree hardware support |
| `snippets/` | Connectivity variants (wifi, thread-sed, …) as Zephyr snippets |
| `include/mcuhome/` | Public C API headers |
| `lib/` | Portable, `native_sim`-testable libraries |
| `compat/` | Headers that bridge a version mismatch between two pinned upstreams (today: mbedTLS 4's moved legacy headers, for connectedhomeip). Each entry names its own deletion condition — see `compat/README.md` |
| `tests/`, `samples/` | Twister suites and samples |
| `tests_py/` | pytest suite of the three Python packages (kept apart from twister's `tests/`) |
| `containers/builder/` | The build-container image (ADR 0007) — the contract's reference implementation |
| `scripts/` | Dev tooling, future custom west extension commands |
| `docs/adr/` | Architecture decision records (MADR-style) — immutable finals at the top level, living drafts in `draft/`; lifecycle in `docs/adr/README.md` (ADR 0021) |

## Non-obvious invariants

- **Dual role:** this repo is BOTH the west manifest repository of the
  workspace AND a reusable Zephyr module. Changes to `west.yml` and
  `zephyr/module.yml` affect both consumption modes.
- **West topdir rule:** the workspace top directory must not be a git repo.
  This repo is cloned one level below it (`workspace/mcuhome/`). Never run
  `git init` in the workspace top directory; never commit workspace siblings
  (`zephyr/`, `modules/`, `.west/`).
- **Pinning policy:** Zephyr and all modules are pinned to tags/SHAs in
  `west.yml`. Bump deliberately, never to `main`. Matter (CHIP) and Zephyr
  versions are bumped as a matched pair (ADR 0006) — currently CHIP
  v1.5.1.0 pinned against Zephyr v4.4.0.
- **Python codegen and C runtime version in lockstep** — that is why they
  share this repo (ESPHome learned this the hard way).
- **Device-class variants are configuration, not code:** WiFi vs Thread FTD
  vs Thread SED is expressed via snippets/Kconfig fragments, never via
  parallel source trees.
- **`app/` is not an application.** It holds the single generic
  application main that `mcuhome build` compiles together with the device
  configuration it generates; `west build mcuhome/app` refuses at CMake
  configure time and says so. Anything device-, board- or
  peripheral-specific is a contract violation there — see `app/README.md`.
- **A device's commissioning identity is emitted by one function.**
  `mcuhome/model/pairing.py::kconfig_lines()` writes all of
  `CONFIG_CHIP_DEVICE_{VENDOR_ID,PRODUCT_ID,DISCRIMINATOR,SPAKE2_PASSCODE,
  SPAKE2_IT,SPAKE2_SALT,SPAKE2_TEST_VERIFIER}` from one tuple, and
  `tests_py/test_pairing.py` asserts no other module even names them. CHIP
  does not check the passcode against the verifier derived from it on
  Zephyr, so anything that could write one without the other yields
  firmware that builds, boots and then refuses every commissioner.
- **A device's version is emitted by one function too.**
  `mcuhome/model/ota.py::kconfig_lines()` writes
  `CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION` together with
  `CONFIG_CHIP_DEVICE_SOFTWARE_VERSION{,_STRING}` from one SemVer string
  (ADR 0015 decision 9). A build in which MCUboot's image version and
  Matter's SoftwareVersion disagree updates to an image the controller then
  reports as the wrong version, and nothing warns.
- **`components/matter/Kconfig`'s `MCUHOME_MATTER_OTA` selects nothing, on
  purpose.** `MCUHOME_MATTER` depends on the PICOLIBC member of the libc
  choice, and a `select FLASH`/`STREAM_FLASH`/`IMG_MANAGER` from under it
  closes a Kconfig cycle through `REBOOT` and `USB_DFU_REBOOT` — which
  stops Kconfig parsing the tree in *every* build in the repository, the
  native_sim suites included. The group is registry data
  (`UpdateSchemeDef.matter_ota_kconfig`) and the C side checks it by name.
- **Credentials are drawn once, into the user's YAML** (`mcuhome
  init-pairing`), never per build — random per device *and* byte-identical
  builds, which is only possible if the configuration is the source
  (yaml-schema.md §4.1).
- **Two files carry the Matter build glue** — `samples/matter-node/
  CMakeLists.txt` and the one `mcuhome/compiler/generate.py` emits — and
  `tests_py/test_generate.py` asserts the shared blocks stay byte-equal.
  A CMake fix found on the bench goes into both, in the same commit.

## Debug output is load-bearing (product-owner directive, until v1.0)

RTT debug output is part of every image this repository produces — every
application, every bootloader, every test firmware. It is never disabled,
removed or reduced silently, not even to win flash or RAM back: space
pressure is reported and resolved as an explicit joint decision with the
product owner, never absorbed by dropping the log. The precedent is
recorded in draft ADR 0015 — an OTA swap failure whose one
explanatory MCUboot log line was compiled out (`CONFIG_LOG=n`) turned a
five-minute diagnosis into a day of source reading.

Concretely:

- New features get their debug output FIRST, and a baseline firmware
  with that output runs before the feature changes behaviour.
- `scripts/check_debug_output.py` (a CI step and a pre-commit hook)
  fails on diagnostics-reducing Kconfig lines in config fragments —
  `CONFIG_LOG=n`, `CONFIG_LOG_MODE_MINIMAL=y`, an RTT backend or console
  switched off, `CONFIG_PRINTK=n`, `CONFIG_ASSERT=n` and similar —
  unless the line carries an explicit approval marker on the same or the
  directly preceding line:

  ```
  # debug-output: approved <short reason, or where the decision is recorded>
  ```

- The marker states a decision; it never creates one. Adding it requires
  the decision to actually exist — an ADR, an in-file rationale, or the
  product owner's explicit sign-off. A marker whose reason nobody can
  trace is a defect.

## Commands

```sh
# One-time workspace setup (full sequence; the workspace dir must not be
# a git repo and must contain this clone as mcuhome/)
mkdir mcuhome-workspace && cd mcuhome-workspace
git clone https://github.com/mcu-home/mcuhome
west init -l mcuhome && west update

# Build the reference sample (from the workspace top directory).
# `mcuhome/app` is NOT buildable — it is the generic main, see below.
west build -p -b nrf7002dk/nrf5340/cpuapp -S matter -S debug-rtt \
  mcuhome/samples/matter-node

# Run the Zephyr test suites (tests/)
west twister -T mcuhome/tests --integration --inline-logs -v

# Builder (Python): install once, then run its tests (tests_py/) — no
# Zephyr and no west workspace needed, ~1 s. The `mcuhome` *command*
# comes from the sibling cli repo (github.com/mcu-home/cli, cloned next
# to this one); the three distributions below are what it calls into.
#
# Three paths and no `.[dev]`: the repository root ships no distribution
# any more (ADR 0020 decision 2 reserves the plain name `mcuhome` for the
# command), and an aggregate that pulled the three from an index would
# undo the editable installs it was asked for. The root pyproject.toml
# says so at the top and keeps the tool configuration.
pip install -e ./packaging/model -e ./packaging/workbench \
            -e ./packaging/compiler 'pytest>=8.0'
pip install -e ../cli
pytest

# The `remote` build method's tests need two more things: the optional
# extra (aiohttp + zstandard) and the build server they drive the client
# against as a real peer. Without them tests_py/test_sessionclient.py
# skips itself with one reason naming what is missing (`pytest -rs`).
pip install -e './packaging/workbench[remote]' -e ../build-server

# Check one device configuration with the builder
mcuhome validate docs/design/examples/00-bmp180-two-endpoints.yaml

# The machine-readable surface (dashboard ADR 0011 "Block 0"): --json on
# validate/build, the registry and the main.yaml JSON Schema as data, and
# a scaffold for a new device. `mcuhome.workbench.api` is the same thing in process.
mcuhome validate <device> --json
mcuhome schema registry
mcuhome new bedroom-climate --board nrf7002dk/nrf5340/cpuapp

# Detached signing (ADR 0015 decision 8): compile without the private key,
# sign where the key is. build-report.json carries the imgtool parameters
# (the default `local` method's §7.2.1 delivery; `--method local-dev`
# writes build-manifest.json instead, and the signer reads either).
mcuhome public-key -o signing.pub
mcuhome build <device> --no-sign --public-key signing.pub
mcuhome sign build/<device>

# Generate the Zephyr application for it and stop (stage 4)
mcuhome build docs/design/examples/00-bmp180-two-endpoints.yaml \
  --build-dir /tmp/bmp180-node --generate-only

# Generate AND compile it (stages 4-5), from the workspace top directory.
# Compiles in the builder image (ADR 0007) — pull it once:
#   docker pull ghcr.io/mcu-home/builder:zephyr-4.4.0-r7
# Writes the application to build/<device>/app and the CMake tree to
# build/<device>/build — one sub-directory per sysbuild image (ADR 0015:
# mcuboot + the signed application) — and reports both with their
# footprints. The first build generates the per-user signing key in
# ~/.config/mcuhome/; --signing-key points somewhere else.
# -S adds a snippet on top of the ones the configuration needs.
mcuhome build mcuhome/docs/design/examples/00-bmp180-two-endpoints.yaml \
  --build-dir build/bmp180-node -S debug-rtt

# The same, on the host toolchain instead of in the container
mcuhome build … --method local-dev

# Build the builder image from source (containers/builder/README.md).
# The context is the repository root, not containers/builder/: since r3
# the image bakes a west workspace, so west.yml and patches/ are inputs.
docker build -t ghcr.io/mcu-home/builder:zephyr-4.4.0-r7 \
  -f containers/builder/Dockerfile .

# Python lint/format
ruff check --fix . && ruff format .

# All lint hooks
pre-commit run --all-files
```

### What a compiling build needs on the host

**git and docker. That is the whole list** (ADR 0007). `mcuhome build`
compiles inside the builder image, which carries the Zephyr SDK, west,
`gn`, `zap` and ccache; the workspace is bind-mounted into it at its own
absolute path, as the calling user, so nothing is left behind owned by
root. `mcuhome validate` and `--generate-only` need even less — Python,
and nothing else. See `containers/builder/README.md`.

Since image revision r3 the image also **carries** a west workspace of
its own at `/mcuhome/workspace` — Zephyr, the modules, MCUboot and the
Matter SDK at the revisions `west.yml` pins, patched, with the manifest
repository's directory left empty for the SDK mount (ADR 0007:
`git describe` in the workspace decides `BUILD_VERSION` and therefore
the firmware bytes, so baking it makes that state a property of the image
digest). Nothing reads it yet: `mcuhome build` still mounts and builds
out of the *host's* workspace, so the host requirement above is unchanged
until the run-time side is switched over.

A missing docker, a stopped daemon and a missing image are three
different refusals with three different fixes, all raised before the
build starts.

`--method local-dev` compiles on the host instead, which is what MCUHome's
own contributors do in this workspace. That path needs a west workspace plus
three things a Zephyr installation does not bring:

| Requirement | Why | Provided by the builder? |
|---|---|---|
| `gn` on `PATH` | the Matter SDK builds its own libraries with GN | no — install it (the image has it) |
| `zap`/`zap-cli` on `PATH`, or `ZAP_INSTALL_PATH` | generates the root-node data model from `components/matter/zap/` | no — install it (the image has it) |
| `PYTHONPATH=<repo>/scripts/pyshim` | CHIP v1.5.1.0 ships without the `python_path` helper its codegen imports (upstream candidate C1) | **yes**, automatically |

Missing tools are reported by name before the build starts, never as a
compiler error ten minutes in. `ZEPHYR_BASE` is also filled in for the
build (west does not export it, and the generated CMakeLists looks for
the Matter SDK next to it) unless it is already set.

CI (`.github/workflows/ci.yml`) runs the lint/licensing checks below plus
the twister suites, the latter inside the same builder image, with the
`matter` west group excluded because every suite in `tests/` is CHIP-free
by design (ADR 0014).

A third job, `matter`, covers what that exclusion leaves uncovered: it
materialises a west workspace **with** the `matter` group, applies both
files in `patches/` with `git apply` (a patch that has drifted from its
pinned upstream fails the job — there is no `--3way`, no fallback), and
runs `mcuhome build` on
`docs/design/examples/00-bmp180-two-endpoints.yaml` in the builder image,
i.e. the container path rather than `--method local-dev`.
`scripts/check_build_artifacts.py` then asserts the artifact set —
MCUboot image, signed application, merged
hex, `build-manifest.json`, every file checked against the size and
SHA-256 the manifest recorded. It exists because three build inputs went
missing at once without CI noticing (`compat/mbedtls/` outside every
repository, the `pigweed_environment.gni` stub in no patch, `cryptography`
absent from the image); the job's head comment in the workflow tells that
story. Because a Matter build costs a quarter of an hour of runner time,
it is triggered by a path gate (`matter-gate`) rather than by every pull
request — the path list, and what is deliberately not on it, is in the
workflow. `workflow_dispatch` runs it on demand.

## Coding standards

- **C:** Zephyr coding style (`.clang-format`, tabs). Static allocation
  preferred; no heap after initialization; bounded stacks; ISR-safe APIs in
  interrupt context. Respect SED power budgets: no busy-waiting, no
  unnecessary wakeups, use kernel primitives and PM hooks.
- **Python:** ruff (lint + format), line length 100, target Python 3.11+.
- **Devicetree over hardcoding:** hardware description belongs in DTS
  overlays and bindings, not in C constants.

## Licensing rules (strict)

- Everything is **Apache-2.0**. Every new file gets SPDX headers (a
  `SPDX-FileCopyrightText` line naming The MCUHome Contributors and an
  `Apache-2.0` license identifier — copy them from any existing file).
  The repo is REUSE-compliant (`reuse lint` runs in pre-commit).
- **Never copy code from GPL projects. ESPHome's C++ runtime is GPLv3 —
  it is inspiration only, never a source.** Zephyr, its modules and
  connectedhomeip are Apache-2.0 and safe to reference.

## Commit and PR conventions

- Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, …) — types
  drive SemVer release automation.
- Every commit is DCO-signed-off: `git commit -s`.
- Default branch is `main`; short-lived `feat/…`, `fix/…` branches.
- Non-trivial design decisions require an ADR **draft** in
  `docs/adr/draft/` (numbered, MADR-style: Context / Decision /
  Consequences). While the component is being built the draft is a
  living document; the final ADR is written from the real result once
  the component is done. Lifecycle: `docs/adr/README.md` (ADR 0021).
