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
canonical device model). Code generation and the container build are not
implemented — `mcuhome build` refuses. Check `docs/adr/` before assuming
any design decision.

## Repository map

| Path | Role |
|---|---|
| `west.yml` | West manifest (T2 topology) — Zephyr + modules, pinned revisions |
| `zephyr/module.yml` | Makes this repo consumable as a Zephyr module |
| `CMakeLists.txt`, `Kconfig` | Zephyr module build entry points |
| `mcuhome/` | Python package: YAML validation, codegen, `mcuhome` CLI |
| `components/` | Components: Python schema + C sources side by side |
| `app/` | Placeholder app (later the codegen target) |
| `boards/`, `drivers/`, `dts/bindings/` | Out-of-tree hardware support |
| `snippets/` | Connectivity variants (wifi, thread-sed, …) as Zephyr snippets |
| `include/mcuhome/` | Public C API headers |
| `lib/` | Portable, `native_sim`-testable libraries |
| `tests/`, `samples/` | Twister suites and samples |
| `tests_py/` | pytest suite of the builder package (kept apart from twister's `tests/`) |
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

## Commands

```sh
# One-time workspace setup (full sequence; the workspace dir must not be
# a git repo and must contain this clone as mcuhome/)
mkdir mcuhome-workspace && cd mcuhome-workspace
git clone https://github.com/mcu-home/mcuhome
west init -l mcuhome && west update

# Build the placeholder app (from the workspace top directory)
west build -p -b native_sim mcuhome/app

# Run the Zephyr test suites (tests/)
west twister -T mcuhome/tests --integration --inline-logs -v

# Builder (Python): install once, then run its tests (tests_py/) — no
# Zephyr and no west workspace needed, ~1 s
pip install -e '.[dev]'
pytest

# Check one device configuration with the builder
mcuhome validate docs/design/examples/00-bmp180-two-endpoints.yaml

# Python lint/format
ruff check --fix . && ruff format .

# All lint hooks
pre-commit run --all-files
```

CI (`.github/workflows/ci.yml`) runs the lint/licensing checks below on
every push and PR. It landed together with the first test suite
(`tests/matter_tables/`); the twister/Zephyr build itself is not wired
into CI yet — see the TODO block in the workflow file.

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
