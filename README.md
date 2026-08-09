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
Home Assistant instance over Thread (design record: see
[docs/adr/](docs/adr/)). The Python YAML builder (phase 2) is under
construction, and now goes end to end: `mcuhome validate <device>`
checks a configuration and prints what it resolves to, and `mcuhome
build <device>` generates the Zephyr application for it and compiles it
into a flashable image, reporting where the image is and what it costs
in flash and RAM. Compiling happens in the versioned builder image
([ADR 0007](docs/adr/0007-containerized-toolchain.md)), so the host needs
nothing but git and docker.
The companion web interface lives in
[mcu-home/dashboard](https://github.com/mcu-home/dashboard).

## Repository layout

This repository has a dual role: it is the **west manifest repository** of
the MCUHome workspace (T2 topology) *and* a reusable **Zephyr module**.

| Path | Purpose |
|---|---|
| `west.yml` | West manifest pinning Zephyr and modules |
| `zephyr/module.yml` | Zephyr module definition (boards, DTS, snippets roots) |
| `mcuhome/` | Python package: YAML config validation, codegen, build orchestration; `mcuhome.api` is the supported surface (the `mcuhome` command is its own repo, [mcu-home/cli](https://github.com/mcu-home/cli)) |
| `components/` | MCUHome components (Python schema + C sources side by side) |
| `app/` | The generic application main every generated device shares |
| `boards/`, `drivers/`, `dts/bindings/` | Out-of-tree Zephyr hardware support |
| `snippets/` | Connectivity/device-class variants (wifi, thread-sed, …) |
| `include/mcuhome/`, `lib/` | Public runtime API and portable libraries |
| `samples/`, `tests/` | Twister-driven samples and test suites |
| `tests_py/` | pytest suite of the builder package |
| `containers/builder/` | The builder image: the one build environment (ADR 0007) |
| `scripts/` | Development tooling and future custom west extension commands |
| `docs/adr/` | Architecture decision records |

## Getting started (developers)

MCUHome uses a [west workspace](https://docs.zephyrproject.org/latest/develop/west/workspaces.html).
The workspace top directory must **not** be a git repository:

```sh
mkdir mcuhome-workspace && cd mcuhome-workspace
git clone https://github.com/mcu-home/mcuhome
west init -l mcuhome
west update
```

Requirements on your machine: **git, docker and Python ≥ 3.11** — no
Zephyr SDK, no cross-compilers, no vendor tools. The toolchain lives in
the MCUHome builder image
([ADR 0007](docs/adr/0007-containerized-toolchain.md)), which is
versioned in lockstep with the pinned Zephyr release:

```sh
docker pull ghcr.io/mcu-home/builder:zephyr-4.4.0-r3
```

Then build a device from its YAML description. The `mcuhome` command is
a thin shell in its own repository
([mcu-home/cli](https://github.com/mcu-home/cli)); until the packages
are published it is installed from a checkout next to this one:

```sh
pip install -e mcuhome            # the builder library
git clone https://github.com/mcu-home/cli
pip install -e cli                # the `mcuhome` command
mcuhome build mcuhome/docs/design/examples/00-bmp180-two-endpoints.yaml \
  --build-dir build/bmp180-node
```

That writes the generated Zephyr application to `build/bmp180-node/app`,
compiles it in the container as your own user, and prints both images —
MCUboot and the application signed for it — with their flash/RAM
footprints and the flash layout they were built against. The first build
also draws your own ECDSA P-256 signing key into
`~/.config/mcuhome/signing.key` and says so: every device you flash
verifies its firmware against it, so it is worth keeping (ADR 0015). `--generate-only` stops after the
generating half, which needs nothing but Python; `--native` compiles on
a host toolchain instead, for people working on MCUHome itself. Details,
including how to build the image yourself, are in
[containers/builder/README.md](containers/builder/README.md).

Build parallelism is auto-detected from CPU count and available RAM (a
Matter compile unit peaks around 1-1.5 GiB, so the formula budgets 2 GiB
per job and never exceeds the core count) — `MCUHOME_JOBS=N` overrides
the auto-detection, `--jobs N` overrides both, and it applies inside the
builder container too, resolved on the host before `docker run`.

### Starting a device from nothing

```sh
mcuhome new bedroom-climate --board nrf7002dk/nrf5340/cpuapp
mcuhome init-pairing bedroom-climate      # this device's commissioning codes
mcuhome validate bedroom-climate
```

`mcuhome new` writes `devices/bedroom-climate/main.yaml` — a complete
configuration with a commented, working hardware example to uncomment.
It never draws commissioning credentials itself: those are drawn once,
by their own command, so that every build of a device is byte-identical
(`docs/design/yaml-schema.md` §4.1).

### Signing where the key is, building where the CPU is

Every image is signed with your own key
([ADR 0015](docs/adr/0015-update-and-partition-architecture.md) decision 8).
Normally that happens during the build. When the machine that compiles
is not the machine that owns the key — a build server, or the future
dashboard's build App — the two are separated:

```sh
mcuhome public-key -o signing.pub          # the half that may travel
mcuhome build <device> --no-sign --public-key signing.pub
mcuhome sign build/<device>                # where the private key is
```

The unsigned build compiles the bootloader with the public key in it and
leaves the application unsigned, and writes `build-manifest.json` stating
the exact `imgtool` parameters (`--version`, `--header-size`,
`--slot-size`, `--align`). `mcuhome sign` reads them back and runs the
same tool with the same arguments — the result is the same image, and
`--no-sign` deliberately leaves no file behind that looks flashable and
is not.

### Machine-readable output

```sh
mcuhome validate <device> --json    # the resolved model, or the errors
mcuhome build    <device> --json    # the build manifest (log on stderr)

# A machine that only compiles takes the resolved model and nothing else:
# no configuration tree, no secrets (dashboard ADR 0007 decision 4). The
# generated tree is byte-identical to the one the direct route produces.
mcuhome build --model device-model.json --build-dir build/<device>
mcuhome schema                      # JSON Schema for main.yaml
mcuhome schema registry             # boards, drivers, clusters, device types
```

### Using the builder from Python

`mcuhome.api` is the supported programmatic surface, and the only part of
the package covered by the SemVer promise of
[ADR 0005](docs/adr/0005-semver-and-conventional-commits.md):

```python
from mcuhome import api

tree, entry = api.find_device("bedroom-climate", config_root=root)
result = api.validate_device(entry, tree=tree)
if result.ok:
    model = result.model  # the canonical device model
else:
    # message, file, line, column, key, hint, kind
    for problem in result.error_dicts():
        print(problem["message"])
```

`validate_device` reports **every** problem rather than raising on the
first, which is what lets an editor show a whole configuration's markers
in one pass. `api.registry_data()` and `api.config_json_schema()` are the
same documents `mcuhome schema` prints; `api.read_manifest()` loads a
build manifest. Everything else in the package is an implementation
detail and may change between releases.

To see the framework run without the builder in the picture, build the
reference sample by hand:

```sh
west build -p -b nrf7002dk/nrf5340/cpuapp -S matter -S debug-rtt mcuhome/samples/matter-node
```

The `matter` and `debug-rtt` snippets are mandatory, not optional. See
[samples/matter-node/README.md](samples/matter-node/README.md) for
hardware prerequisites and wiring. Note that `mcuhome/app` is *not* a
buildable application — it holds the generic main the builder compiles
into every generated device ([app/README.md](app/README.md)).

## Relationship to ESPHome

MCUHome is inspired by ESPHome's YAML-first user experience but shares no
code with it. ESPHome's C++ runtime is GPLv3; MCUHome is Apache-2.0 and must
stay clean of GPL code — see [CONTRIBUTING.md](CONTRIBUTING.md).

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Questions and ideas go to
[GitHub Discussions](https://github.com/mcu-home/mcuhome/discussions);
bug reports to the [issue tracker](https://github.com/mcu-home/mcuhome/issues).

## License

Apache License 2.0 — see [LICENSE](LICENSE). This repository follows the
[REUSE](https://reuse.software/) specification; every file carries SPDX
license information.
