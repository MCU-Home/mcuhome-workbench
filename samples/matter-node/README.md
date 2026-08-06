# matter-node

Minimal Matter node on vanilla Zephyr with upstream CHIP v1.5.1.0: a
**native composed node** (ADR 0014) whose sensor endpoints are registered
at runtime **directly under the root node** — no aggregator, no bridge.
The static data model is the framework ZAP
([`components/matter/zap/`](../../components/matter/zap/)), which contains
endpoint 0 and nothing else.

| Endpoint | Device type | Cluster | Source |
|---|---|---|---|
| EP1 | Temperature Sensor `0x0302` | Temperature Measurement `0x0402` | BMP180 die temperature |
| EP2 | Pressure Sensor `0x0305` | Pressure Measurement `0x0403` | BMP180 pressure |

Both endpoints are siblings under the root, both are fed by the generic
sensor channel adapter ([`components/sensor/`](../../components/sensor/)),
and both report `null` until a reading exists — the node boots,
commissions and stays commissioned with no sensor attached at all.

This is the sample that proved the builder's core mechanism end to end:
commissioned into a production Home Assistant over Thread, the endpoint
shows up as a normal HA sensor entity.

## Hardware: wiring the BMP180 to the nRF7002-DK

The sensor sits on the DK's Arduino-header I2C bus. Four wires — but
read the voltage warning first.

> **VOLTAGE WARNING — the nRF7002 DK is a 1.8 V board.** Per Nordic's
> hardware guide (doc 4486_138 §4.4.2), the DK's `VDD` rail — including
> the Arduino power-header pin where a classic Arduino has 3V3 — is
> **1.8 V**, and the GPIOs tolerate little above that. A bare BMP180
> (1.62–3.6 V per datasheet) is fine on `VDD` directly. A typical GY-68
> breakout with its own 3.3 V regulator, however, needs the `5V` pin and
> then presents ~3.3 V I2C logic — **which can permanently damage the
> nRF5340's pins**. Such modules require a bidirectional level shifter.
> Check which module you have before wiring anything.

> **Naming hazard, read this first.** Nordic labels the DK's *connectors*
> `P1`…`P17`, and the nRF5340's *GPIO ports* are also called `P0`/`P1`.
> Connector `P1` is the power/ground header; GPIO pins `P1.02`/`P1.03` are
> the I2C lines and are **not** on it. Whenever this section says `P1.02`
> it means the GPIO.

| BMP180 breakout | nRF7002-DK | nRF5340 GPIO |
|---|---|---|
| `VIN` / `VCC` / `3V3` | `VDD` (1.8 V!) on connector **`P1`** (power/GND header) — see voltage warning above | — |
| `GND` | any `GND` on connector **`P1`** | — |
| `SDA` | the pin silkscreened **`SDA`** on the 10-pin digital header (far end, next to `AREF`) | `P1.02` |
| `SCL` | the pin silkscreened **`SCL`**, immediately next to `SDA` | `P1.03` |

**Do not use `A4`/`A5`.** On classic Arduino boards those double as
SDA/SCL. On the nRF7002-DK they are separate GPIOs — `A4 = P0.25 (AIN4)`,
`A5 = P0.26 (AIN5)` — with no connection to the I2C controller. Use the
pins actually labelled `SDA` and `SCL`.

Where this comes from:

- Vanilla Zephyr v4.4.0,
  `boards/nordic/nrf7002dk/nrf5340_cpuapp_common.dtsi`: `i2c1` carries the
  label `arduino_i2c`, and the `arduino_header` `gpio-map` puts
  `ARDUINO_HEADER_R3_D14` on `&gpio1 2` and `ARDUINO_HEADER_R3_D15` on
  `&gpio1 3`.
- Vanilla Zephyr v4.4.0,
  `boards/nordic/nrf7002dk/nrf5340_cpuapp_common_pinctrl.dtsi`:
  `i2c1_default` assigns `TWIM_SDA` to `P1.02`, `TWIM_SCL` to `P1.03`.
  Hence `D14 = SDA = P1.02` and `D15 = SCL = P1.03`.
- Nordic's *nRF7002 DK Hardware User Guide*: GPIO is exposed on connectors
  `P2`–`P6` (and `P24`), while "the `P1` connector provides access to
  ground and power"; the analog-pin mapping page confirms `A4 = P0.25`,
  `A5 = P0.26`. The exact pin *position* of `SDA`/`SCL` within the digital
  header follows the Arduino UNO R3 mechanical standard (`SCL, SDA, AREF,
  GND, D13 …`), which the DK implements — and both pins are silkscreened.

### Pull-ups — the one thing that will bite

The DK **does** fit I2C pull-up resistors, but they are **switched, not
wired**: two analog switches connect them to SDA/SCL only when shield
detection fires, which happens when a real Arduino shield grounds the
detect pin. **A breakout wired with jumper wires does not trigger it**, so
the onboard pull-ups stay disconnected. Options:

1. Use a breakout that carries its own pull-ups (most BMP180 modules do,
   typically 4.7 kΩ) — nothing else to do.
2. Or fit two external pull-ups (4.7 kΩ to `VDD`).
3. Or short solder bridge **`SB32`** ("Short to permanently enable the I2C
   pull-up resistors"). `SB33` is the counterpart — closed by default, cut
   to permanently disable them.

Do **not** substitute the nRF5340's internal pull-ups: they are far too
weak for an I2C bus and only ever half-work below 100 kHz.

Two more:

- **Address** `0x77`, fixed in silicon. The BMP180 has no address-select
  pin, so two of them cannot share a bus.
- **A missing or unreachable sensor is not an error.** The node boots,
  commissions and stays commissioned; the log shows one `mcuhome_sensor`
  warning per channel and both MeasuredValue attributes read as `null`.
  Attaching the sensor and rebooting is enough — no reconfiguration, no
  re-commissioning. Which also means: check the log, do not conclude the
  wiring is fine just because the node came up.

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
| `src/mcuhome_config.c` | **Golden file.** The device's Matter model as plain-C tables (`mcuhome_node_config`): two endpoints, one cluster each, three attributes each. Its *shape* is the contract for builder phase-2 codegen, and it doubles as that phase's regression fixture — keep it in lockstep with `include/mcuhome/matter_tables.h`. |
| `src/main.c` | Application glue: LEDs, `mcuhome_matter_start()`, the channel/binding tables handed to `mcuhome_sensor_start()`, and an override of the `mcuhome_matter_stage()` hook for LED status. Plain C — generated app glue never needs a C++ toolchain. Nothing here polls, converts or publishes: that is the channel layer's job. |
| `boards/nrf7002dk_nrf5340_cpuapp.overlay` | The BMP180 devicetree node on `arduino_i2c`, plus the prototype entropy source. Hardware belongs in DTS, never in C constants. |
| `include/CHIPProjectConfig.h` | One-line wrapper around the framework's `<mcuhome/matter/chip_project_config.h>`; exists only because CHIP resolves `CONFIG_CHIP_PROJECT_CONFIG` relative to the application directory. |

The `nrf52840dongle/nrf52840` build has no sensor: `src/main.c` guards the
binding table with `DT_NODE_HAS_STATUS_OKAY()` and simply skips
`mcuhome_sensor_start()` there. Generated firmware targets exactly one
board and will never carry that guard — it is the price of a sample that
builds for two.

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
