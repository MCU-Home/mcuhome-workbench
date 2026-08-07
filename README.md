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
construction: `mcuhome validate <device>` checks a configuration and
prints what it resolves to, and `mcuhome build <device> --generate-only`
writes the Zephyr application for it — compiling that application is the
next step.
The companion web interface lives in
[mcu-home/dashboard](https://github.com/mcu-home/dashboard).

## Repository layout

This repository has a dual role: it is the **west manifest repository** of
the MCUHome workspace (T2 topology) *and* a reusable **Zephyr module**.

| Path | Purpose |
|---|---|
| `west.yml` | West manifest pinning Zephyr and modules |
| `zephyr/module.yml` | Zephyr module definition (boards, DTS, snippets roots) |
| `mcuhome/` | Python package: YAML config validation, codegen, builder CLI |
| `components/` | MCUHome components (Python schema + C sources side by side) |
| `app/` | Placeholder application (later: codegen target) |
| `boards/`, `drivers/`, `dts/bindings/` | Out-of-tree Zephyr hardware support |
| `snippets/` | Connectivity/device-class variants (wifi, thread-sed, …) |
| `include/mcuhome/`, `lib/` | Public runtime API and portable libraries |
| `samples/`, `tests/` | Twister-driven samples and test suites |
| `tests_py/` | pytest suite of the builder package |
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
west build -b native_sim mcuhome/app
```

Requirements: Python ≥ 3.11, `west`, and the Zephyr SDK matching the pinned
Zephyr release (v4.4.0). See the
[Zephyr Getting Started Guide](https://docs.zephyrproject.org/latest/develop/getting_started/index.html)
for toolchain setup.

The `native_sim` build above is only a module smoke test — no hardware
involved. To see the framework actually run a Matter node, build the
real sample instead:

```sh
west build -p -b nrf7002dk/nrf5340/cpuapp -S matter -S debug-rtt mcuhome/samples/matter-node
```

The `matter` and `debug-rtt` snippets are mandatory, not optional. See
[samples/matter-node/README.md](samples/matter-node/README.md) for
hardware prerequisites and wiring.

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
