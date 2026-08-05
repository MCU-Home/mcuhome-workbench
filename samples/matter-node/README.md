# matter-node

Minimal Matter node on vanilla Zephyr with upstream CHIP v1.5.1.0: a
**native composed node** (ADR 0014) whose single temperature endpoint is
registered at runtime as **EP1, directly under the root node** — no
aggregator, no bridge. The static data model is the framework ZAP
([`components/matter/zap/`](../../components/matter/zap/)), which contains
endpoint 0 and nothing else.

This is the sample that proved the builder's core mechanism end to end:
commissioned into a production Home Assistant over Thread, the endpoint
shows up as a normal HA sensor entity.

## Build

```sh
# from the west workspace top directory
west build -p -b nrf7002dk/nrf5340/cpuapp -S matter -S debug-rtt mcuhome/samples/matter-node
```

The two snippets are **not optional**:

- `-S matter` — the numeric/choice Kconfig values the Matter stack needs
  (mbedTLS heap, main/workqueue stacks, p256-m + bignum assembly, picolibc)
  plus the nRF53 802.15.4 workqueue sizing.
- `-S debug-rtt` — the RTT log transport (the board has no free UART
  console in this configuration) including the boot-time RTT control-block
  re-init.

Leaving `-S matter` out is refused at CMake configure time by
`components/matter/CMakeLists.txt`, which names the snippet; the
`BUILD_ASSERT`s in `components/matter/src/matter_init.cpp` are the
backstop. Without that guard the image compiles fine and then dies at
runtime with no console to explain why.

`nrf52840dongle/nrf52840` is also in `platform_allow`; it uses LED status
instead of a console (see `boards/nrf52840dongle_nrf52840.conf`).

## What this sample still contains

Almost nothing — which is the point of ADR 0014. The Matter runtime moved
into the framework component (`components/matter/`), so what is left is
the shape the builder will generate:

| File | Role |
|---|---|
| `src/mcuhome_config.c` | **Golden file.** The device's Matter model as plain-C tables (`mcuhome_node_config`): one endpoint, one cluster, three attributes. Its *shape* is the contract for builder phase-2 codegen, and it doubles as that phase's regression fixture — keep it in lockstep with `include/mcuhome/matter_tables.h`. |
| `src/main.c` | Application glue: LEDs, `mcuhome_matter_start()`, a simulated sensor writing its attribute store cell, and an override of the `mcuhome_matter_stage()` hook for LED status. Plain C — generated app glue never needs a C++ toolchain. |
| `include/CHIPProjectConfig.h` | One-line wrapper around the framework's `<mcuhome/matter/chip_project_config.h>`; exists only because CHIP resolves `CONFIG_CHIP_PROJECT_CONFIG` relative to the application directory. |

Neither C file contains a CHIP or ember include, an external attribute
callback, or a stack lock. If a future change adds one, that change
belongs in `components/matter/` instead.

## Environment prerequisites

CHIP's build runs code generators that a plain Zephyr environment does not
provide:

| Requirement | Why |
|---|---|
| `PYTHONPATH=<workspace>/mcuhome/scripts/pyshim` | CHIP v1.5.1.0 ships without the `python_path` helper its codegen scripts import (upstream candidate C1) — see [`scripts/pyshim/`](../../scripts/pyshim/) |
| `zap` / `zap-cli` on `PATH` (or `ZAP_INSTALL_PATH` set) | ZAP generates `endpoint_config.h` & friends from the framework `.zap` at build time |
| Zephyr SDK on `PATH` / `ZEPHYR_SDK_INSTALL_DIR` set | ARM cross toolchain |

```sh
export PYTHONPATH=$PWD/mcuhome/scripts/pyshim
export ZAP_INSTALL_PATH=<path to zap install>
export PATH="$ZAP_INSTALL_PATH:$PATH"
```

## Insecure entropy — development only

The nRF5340 application core has no upstream entropy driver, so the build
uses MCUHome's placeholder PRNG
([`drivers/entropy/`](../../drivers/entropy/)). It is gated behind
`CONFIG_MCUHOME_ALLOW_INSECURE_ENTROPY`, set explicitly in
`boards/nrf7002dk_nrf5340_cpuapp.conf`. That gate is a deliberate build
barrier: without it the build fails, so the placeholder cannot slip into a
release unnoticed. Never set it in a shipping configuration.
