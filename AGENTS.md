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
`containers/builder/`), or on the host with `--native`. Note the
consequence of ADR 0014:
`samples/matter-node/src/mcuhome_config.{c,h}` **is generator output**
and the pytest suite compares it byte for byte — never hand-edit it,
regenerate it. Check `docs/adr/` before assuming any design decision.

## Repository map

| Path | Role |
|---|---|
| `west.yml` | West manifest (T2 topology) — Zephyr + modules, pinned revisions |
| `zephyr/module.yml` | Makes this repo consumable as a Zephyr module |
| `CMakeLists.txt`, `Kconfig` | Zephyr module build entry points |
| `mcuhome/` | Python package: YAML validation, codegen, build orchestration, `mcuhome` CLI |
| `components/` | Components: Python schema + C sources side by side |
| `app/` | The generic application main every generated device shares — **not** a buildable app |
| `boards/`, `drivers/`, `dts/bindings/` | Out-of-tree hardware support |
| `snippets/` | Connectivity variants (wifi, thread-sed, …) as Zephyr snippets |
| `include/mcuhome/` | Public C API headers |
| `lib/` | Portable, `native_sim`-testable libraries |
| `tests/`, `samples/` | Twister suites and samples |
| `tests_py/` | pytest suite of the builder package (kept apart from twister's `tests/`) |
| `containers/builder/` | The builder image (ADR 0007) — the one build environment |
| `scripts/` | Dev tooling, future custom west extension commands |
| `docs/adr/` | Architecture decision records (MADR-style) |

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
  `mcuhome/pairing.py::kconfig_lines()` writes all of
  `CONFIG_CHIP_DEVICE_{VENDOR_ID,PRODUCT_ID,DISCRIMINATOR,SPAKE2_PASSCODE,
  SPAKE2_IT,SPAKE2_SALT,SPAKE2_TEST_VERIFIER}` from one tuple, and
  `tests_py/test_pairing.py` asserts no other module even names them. CHIP
  does not check the passcode against the verifier derived from it on
  Zephyr, so anything that could write one without the other yields
  firmware that builds, boots and then refuses every commissioner.
- **Credentials are drawn once, into the user's YAML** (`mcuhome
  init-pairing`), never per build — random per device *and* byte-identical
  builds, which is only possible if the configuration is the source
  (yaml-schema.md §4.1).
- **Two files carry the Matter build glue** — `samples/matter-node/
  CMakeLists.txt` and the one `mcuhome/generate.py` emits — and
  `tests_py/test_generate.py` asserts the shared blocks stay byte-equal.
  A CMake fix found on the bench goes into both, in the same commit.

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
# Zephyr and no west workspace needed, ~1 s
pip install -e '.[dev]'
pytest

# Check one device configuration with the builder
mcuhome validate docs/design/examples/00-bmp180-two-endpoints.yaml

# Generate the Zephyr application for it and stop (stage 4)
mcuhome build docs/design/examples/00-bmp180-two-endpoints.yaml \
  --build-dir /tmp/bmp180-node --generate-only

# Generate AND compile it (stages 4-5), from the workspace top directory.
# Compiles in the builder image (ADR 0007) — pull it once:
#   docker pull ghcr.io/mcu-home/builder:zephyr-4.4.0-r1
# Writes the application to build/<device>/app and the CMake tree to
# build/<device>/build — one sub-directory per sysbuild image (ADR 0015:
# mcuboot + the signed application) — and reports both with their
# footprints. The first build generates the per-user signing key in
# ~/.config/mcuhome/; --signing-key points somewhere else.
# -S adds a snippet on top of the ones the configuration needs.
mcuhome build mcuhome/docs/design/examples/00-bmp180-two-endpoints.yaml \
  --build-dir build/bmp180-node -S debug-rtt

# The same, on the host toolchain instead of in the container
mcuhome build … --native

# Build the builder image from source (containers/builder/README.md)
docker build -t ghcr.io/mcu-home/builder:zephyr-4.4.0-r1 containers/builder

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

A missing docker, a stopped daemon and a missing image are three
different refusals with three different fixes, all raised before the
build starts.

`--native` compiles on the host instead, which is what MCUHome's own
contributors do in this workspace. That path needs a west workspace plus
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
- Non-trivial design decisions require an ADR in `docs/adr/` (numbered,
  MADR-style: Context / Decision / Consequences).
