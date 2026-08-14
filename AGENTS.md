# AGENTS.md — MCUHome workbench (flagship / tools repository)

Guide for AI coding agents (and new human contributors) working in this
repository.

## What this project is

MCUHome is an open-source alternative to ESPHome: users describe a smart
home device in YAML, the builder produces Zephyr-based firmware over
Matter/Thread/WiFi. This repository is the **flagship** repo: the
project-wide architecture decisions (`docs/adr/`) and
`mcuhome.workbench` — everything between a parsed device configuration
and something a compiler can be handed: parse, validate, resolve, create
the build context, and drive a build (locally, in a build container, or
through a build server) behind one interface, plus client-side firmware
signing. It publishes one distribution, `mcuhome-workbench`.

## The split (ADR 0024)

Two repositories, one PEP 420 namespace (`mcuhome.*`), one shared ADR
number sequence:

| Repo | Contents | Distributions |
|---|---|---|
| **This repo** (`mcu-home/mcuhome`) | `mcuhome.workbench`, project-wide + tools-shaped ADRs, community files | `mcuhome-workbench` |
| [`mcu-home/mcuhome-sdk`](https://github.com/mcu-home/mcuhome-sdk) | West manifest + Zephyr module, C runtime, `components/`, boards, samples, `patches/`, the build-container definition, `mcuhome.model` + `mcuhome.compiler`, SDK-shaped ADRs, `docs/design/` | `mcuhome-model`, `mcuhome-compiler` |

**Nothing C/Zephyr/west/twister/Matter/container-shaped lives here any
more.** For any of that, read
[mcu-home/mcuhome-sdk's AGENTS.md](https://github.com/mcu-home/mcuhome-sdk/blob/main/AGENTS.md)
instead — this file only covers the workbench.

Check `docs/adr/` — immutable finals at the top level, living drafts in
`draft/` (ADR 0021) — before assuming any design decision; its
[README.md](docs/adr/README.md) also indexes the ADRs that live on the
SDK side of the split.

## Repository map

| Path | Role |
|---|---|
| `pyproject.toml` | Project file for `mcuhome-workbench` — the whole distribution builds from the repository root (ADR 0024) |
| `mcuhome/workbench/` | The one subpackage this repo publishes, part of the PEP 420 `mcuhome.*` namespace shared with `mcuhome-model`/`mcuhome-compiler` in mcuhome-sdk. `mcuhome.workbench.api` is the supported programmatic surface |
| `tests_py/` | pytest suite for this package (kept apart from twister's `tests/`, which lives in the SDK repo) |
| `docs/adr/` | Architecture decision records (MADR-style) — immutable finals at the top level, living drafts in `draft/`; lifecycle in `docs/adr/README.md` (ADR 0021) |
| `.github/` | CI, issue templates, CODEOWNERS |
| `.claude/` | Claude Code project settings |

## The supported programmatic surface: `mcuhome.workbench.api`

**This module is the API. Everything else in the package is an
implementation detail** covered by no compatibility promise. Names
exported here are covered by the project's SemVer promise (draft ADR
0005): they do not change shape within a major version. The `mcuhome`
command line ([mcu-home/cli](https://github.com/mcu-home/cli)) is a thin
shell over it; the dashboard imports it in-process (dashboard ADR 0011
decision 1) — a one-directional dependency: the dashboard declares the
workbench versions it supports and follows its releases, and the
workbench never learns that a dashboard exists.

What is there, roughly front to back: `open_config_tree`/`find_device`
(where a device's configuration lives), `load_model`/`read_model`
(stages 1-3, or the canonical model back from JSON), `validate_device`
(the same stages, returning **every** problem instead of raising on the
first), `error_dicts` (those errors as plain dicts: message, file, line,
column, key, hint, kind), `registry_data`/`config_json_schema` (hardware
and Matter knowledge, and the `main.yaml` schema, as data), and
`read_manifest`. `run_build`/`BuildRequest`/`BuildOutcome` are the three
build methods behind one awaitable call (E64); `resolve_method` turns a
name — a CLI `--method`, `MCUHOME_BUILD_METHOD`, or nothing — into one of
`LOCAL`, `LOCAL_DEV`, `REMOTE` (default `local`; `--native` was removed
per E62).

## Build methods and the remote method's client

`mcuhome.workbench.buildmethods` dispatches the three build methods
behind `run_build`: `local-dev` compiles on the caller's own machine,
`local` drives a build container through the invocation ABI,
`remote` drives a build server through the session protocol
(`mcuhome.workbench.sessionclient`, ADR 0019's eleven verbs — comparison
duty E37, tar.zst context transport). All three deliver an **unsigned**
image plus a build report; nothing here signs anything.

`sessionclient` needs the `remote` extra (`aiohttp` + `zstandard`):

```sh
pip install -e '.[remote]'
```

`tests_py/test_sessionclient.py` goes further: it drives the client
against the **real** `mcuhome-build-server` over a real socket rather
than a mock, so it also needs that peer installed —
`pip install -e ../build-server` (cloned next to this repo). Without
either the extra or the peer, that one file skips itself with a single
reason naming exactly what is missing (`pytest -rs` shows it).

## Non-obvious invariants

- **The private signing key never leaves the local machine.** It is a
  purely client-side secret and appears in no build-method parameter,
  no context file and no wire frame — not as a value, not as a path. The
  only key any build method ever sees is a public half (`signing.pub`,
  or the bootloader's public key). Signing is a single host-side step
  (`mcuhome.workbench.signing`) that runs *after* the artifacts are
  back, over the `build-report.json` every method produces the same way
  (ADR 0015 decision 8, ADR 0018's 2026-08-09 amendment).
- **The workbench never depends on the compiler at import time.** ADR
  0020 decision 3 forbids it (a dashboard install must not carry a
  toolchain, ADR 0017 §2): the edge to `mcuhome.compiler` is resolved
  through `importlib.import_module` at call time and refuses in words
  when the distribution is absent, in both `buildmethods` and
  `sessionclient`. `tests_py/test_packaging_workbench.py` reads the dependency
  arrows out of the syntax tree, so a plain `import` there is a test
  failure, not a style nit.
- **Cross-repository version edges are `~=X.Y.0`** (PEP 440, same
  major.minor family) from v1.0 on — the same rule the CLI uses toward
  the workbench (cli ADR 0002). Before v1.0, editable checkouts.

## Commands

```sh
# Dev install: this repo's own distribution plus its two SDK-side
# siblings, cloned next to it — mcuhome-compiler provides the
# `local`/`local-dev` build methods, mcuhome-model is the one hard
# dependency of mcuhome-workbench.
git clone https://github.com/mcu-home/mcuhome-sdk ../mcuhome-sdk
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../mcuhome-sdk/packaging/model \
            -e ../mcuhome-sdk/packaging/compiler \
            -e '.[remote]'
pytest

# The remote build method's own suite needs a live peer too (see above).
git clone https://github.com/mcu-home/build-server ../build-server
pip install -e ../build-server

# Lint/format
ruff check --fix . && ruff format .

# All lint hooks (pre-commit)
pre-commit run --all-files
```

## Coding standards

- **Python:** ruff (lint + format), line length 100, target Python
  3.11+ — settings in `pyproject.toml`.

## Licensing rules (strict)

- Everything is **Apache-2.0**. Every new file gets SPDX headers (a
  `SPDX-FileCopyrightText` line naming The MCUHome Contributors and an
  `Apache-2.0` license identifier — copy them from any existing file;
  Markdown files are covered by `REUSE.toml`'s fallback annotation
  instead and need no inline header). The repo is REUSE-compliant
  (`reuse lint` runs in pre-commit).
- **Never copy code from GPL projects. ESPHome's C++ runtime is GPLv3 —
  it is inspiration only, never a source.**

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
  This is the flagship repo, so **project-wide decisions live here**;
  SDK-shaped decisions go to `mcu-home/mcuhome-sdk` instead.
