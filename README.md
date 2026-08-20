# MCUHome

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Status: phase 1 complete](https://img.shields.io/badge/status-phase_1_complete-yellow.svg)](#project-status)

**MCUHome turns YAML device descriptions into Zephyr-based smart home
firmware — built on standard protocols instead of a custom API.**

MCUHome is an open-source firmware framework in the spirit of
[ESPHome](https://esphome.io/), rebuilt from the ground up on a different
stack:

| | MCUHome | ESPHome |
|---|---|---|
| RTOS / build system | [Zephyr RTOS](https://zephyrproject.org/) + west | Arduino / ESP-IDF via PlatformIO |
| Network protocols | CoAP and [Matter](https://csa-iot.org/all-solutions/matter/) | custom native API (protobuf) |
| Transports | WiFi **and Thread**, incl. Sleepy End Devices (SED) | WiFi, Ethernet, BT proxy |
| Hardware scope | Everything Zephyr supports (nRF, ESP32, STM32, …) | Espressif-centric, plus RP2040 et al. |

You describe a device in YAML; the MCUHome builder composes Zephyr
configuration, generates the glue code and produces a flashable image. Thanks
to Matter, devices work with Home Assistant and every other Matter
controller out of the box — no custom integration required.

## Project status

**Phase 1 complete.** The firmware runtime — tables-contract framework,
channel layer, netcore entropy service, and a BMP180 two-endpoint sample
— is implemented and hardware-verified: commissioned into a production
Home Assistant instance over Thread. The Python YAML builder (phase 2)
goes end to end: validating a device configuration and compiling it into
a flashable image, reporting where the image is and what it costs in
flash and RAM. That build pipeline — parsing, validation, code
generation, the three build methods and client-side signing — is
`mcuhome.workbench`, published from **this repository**. The firmware,
the C runtime and the west workspace that consumes it live in
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk); the
companion web interface lives in
[mcu-home/mcuhome-ui](https://github.com/mcu-home/mcuhome-ui).

## This repository: the workbench

Since the repository split (draft [ADR 0024](docs/adr/draft/0024-sdk-and-tools-repositories.md))
this is the MCUHome **flagship** repository. It holds:

- **`mcuhome.workbench`** (distribution `mcuhome-workbench`) — everything
  between a parsed YAML device configuration and something a compiler can
  be handed: parse, validate, resolve, create the build context, and
  drive a build behind one interface — locally, in a build container, or
  through a build server (the three build methods of
  [ADR 0020](docs/adr/0020-package-layout-and-the-asynchronous-library.md)
  decision 6) — plus client-side firmware signing.
  **`mcuhome.workbench.api`** is the supported programmatic surface, and
  the only part of this package covered by the SemVer promise of
  [draft ADR 0005](docs/adr/draft/0005-semver-and-conventional-commits.md).
- **The project-wide architecture decision records** ([docs/adr/](docs/adr/))
  — one number sequence shared with the SDK repository; see
  [docs/adr/README.md](docs/adr/README.md) for the split index.
- The community files (license, code of conduct, security policy, …).

| Path | Purpose |
|---|---|
| `pyproject.toml` | Project file for `mcuhome-workbench` — the whole distribution builds from the repository root |
| `mcuhome/workbench/` | The one subpackage this repo publishes — part of the PEP 420 `mcuhome.*` namespace shared with `mcuhome-model`/`mcuhome-compiler` in mcuhome-sdk |
| `tests/python/` | pytest suite for this package |
| `docs/adr/` | Architecture decision records |

## Firmware and the west workspace

The Zephyr west manifest, the C runtime, the build-container definition
and the `mcuhome.model`/`mcuhome.compiler` packages live in
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk) — start
there to build actual firmware; its
[README](https://github.com/mcu-home/mcuhome-sdk#readme) covers the west
workspace setup and the `mcuhome` command-line walkthrough.

## Other repositories

| Repo | What it is |
|---|---|
| [mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk) | West manifest, Zephyr module, C runtime, build-container definition, `mcuhome-model` + `mcuhome-compiler` |
| [mcu-home/mcuhome-cli](https://github.com/mcu-home/mcuhome-cli) | The `mcuhome` command line — a thin shell over this repository's workbench |
| [mcu-home/mcuhome-ui](https://github.com/mcu-home/mcuhome-ui) | The web interface |
| [mcu-home/mcuhome-buildserver](https://github.com/mcu-home/mcuhome-buildserver) | The remote-build orchestrator the `remote` build method talks to |

## Getting started (developers)

This repository is plain Python — no west workspace, no Zephyr SDK, no
docker required to work on it. Clone
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk) next to
it for the two packages the workbench builds on:

```sh
git clone https://github.com/mcu-home/mcuhome-workbench
git clone https://github.com/mcu-home/mcuhome-sdk
cd mcuhome
python3 -m venv .venv && . .venv/bin/activate
pip install -e ../mcuhome-sdk/packaging/model \
            -e ../mcuhome-sdk/packaging/compiler \
            -e '.[remote]'
pytest
```

The `remote` extra pulls in the session-protocol client's dependencies
(`aiohttp`, `zstandard`); its own test file needs a live
`mcu-home/mcuhome-buildserver` peer too and skips itself with one reason if it
is missing.

## Using the workbench from Python

```python
import os
from pathlib import Path

from mcuhome.workbench import api

project, entry = api.find_device("bedroom-climate", env=dict(os.environ), cwd=Path.cwd())
result = api.validate_device(entry, project=project)
if result.ok:
    model = result.model  # the canonical device model
else:
    # message, file, line, column, key, hint, kind
    for problem in result.error_dicts():
        print(problem["message"])
```

The environment and the working directory are *stated* — the workbench
never reads them from the process (ADR 0020), so a server embedding it
can answer for several sessions at once. `api.resolve_project` and
`api.resolve_settings` are the same two steps on their own: where the
project is (the `.mcuhome-project-root` marker, ADR 0022), and what its
five-layer configuration resolves to.

`validate_device` reports **every** problem rather than raising on the
first, which is what lets an editor show a whole configuration's markers
in one pass. `api.registry_data()` and `api.config_json_schema()` are the
same documents the `mcuhome schema` command prints; `api.read_manifest()`
loads a build manifest, and `api.run_build()` drives any of the three
build methods behind one awaitable call. Everything else in this package
is an implementation detail and may change between releases.

## Relationship to ESPHome

MCUHome is inspired by ESPHome's YAML-first user experience but shares no
code with it. ESPHome's C++ runtime is GPLv3; MCUHome is Apache-2.0 and must
stay clean of GPL code — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Questions and ideas go to
[GitHub Discussions](https://github.com/mcu-home/mcuhome-workbench/discussions);
bug reports to the [issue tracker](https://github.com/mcu-home/mcuhome-workbench/issues).

## License

Apache License 2.0 — see [LICENSE](LICENSE). This repository follows the
[REUSE](https://reuse.software/) specification; every file carries SPDX
license information.
