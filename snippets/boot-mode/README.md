<!--
SPDX-FileCopyrightText: 2026 The MCUHome Contributors
SPDX-License-Identifier: Apache-2.0
-->

# boot-mode snippet

Adds the retention area that Zephyr's **boot-mode API** needs, on the two
MCUHome target boards whose upstream devicetree does not define one:
`nrf7002dk/nrf5340/cpuapp` and `nrf52840dongle/nrf52840` (both variants).

Nine lines of devicetree per board — a `zephyr,retention` node of one byte
inside `GPREGRET1`, plus the `zephyr,boot-mode` chosen entry — copied in
shape from the in-tree boards that ship it
(`zephyr/boards/nordic/nrf52840dk/nrf52840dk_nrf52840.dts`,
`zephyr/boards/nordic/nrf5340dk/nrf5340dk_nrf5340_cpuapp.dts`).

## What it is for

Entering a bootloader's recovery mode normally means holding a button
while power-cycling. Neither of these boards makes that a good end-user
story: the dongle is plugged straight into a USB port, and a deployed node
may be somewhere a hand cannot reach. The boot-mode retention register is
the buttonless alternative — the application writes it and reboots
(`bootmode_set(BOOT_MODE_TYPE_BOOTLOADER)`), and MCUboot reads it on the
next boot (`CONFIG_BOOT_SERIAL_BOOT_MODE`) and stays in recovery instead of
chain-loading the application. Remotely, the same request arrives as an
SMP reset with `boot_mode 1` (`CONFIG_MCUMGR_GRP_OS_RESET_BOOT_MODE`).

`GPREGRET1` survives a soft reset and is cleared by a power-on reset,
which is exactly the lifetime a one-shot "boot into recovery" request
wants. On the non-`bare` dongle target it also stays clear of the factory
Open Bootloader, which uses `GPREGRET` (register 0) for its own DFU magic.

## Devicetree only, deliberately

The snippet adds no Kconfig, because the Kconfig side is not the same on
both sides of the pair:

- in the **bootloader** image, `CONFIG_BOOT_SERIAL_BOOT_MODE` selects
  `RETENTION_BOOT_MODE` itself;
- in the **application** image, whoever calls `bootmode_set()` enables
  `CONFIG_RETENTION_BOOT_MODE=y` (and its `RETENTION`/`RETAINED_MEM`
  dependencies) — with mcumgr, `CONFIG_MCUMGR_GRP_OS_RESET_BOOT_MODE=y`
  does it.

Applying a devicetree node costs nothing when nothing uses it, so the same
snippet serves both images.

## Usage

Under sysbuild every image is named explicitly, because sysbuild hands a
bare `-S` to all of them:

```sh
west build -b nrf52840dongle/nrf52840 --sysbuild <app> -- \
    -D<app>_SNIPPET=boot-mode -Dmcuboot_SNIPPET=boot-mode
```

Both are needed for the mechanism to work end to end: the retention area
has to be at the same address in both images, which is what sharing one
devicetree fragment guarantees.

## Status

Applied by `mcuhome build` to both images of every board whose update
scheme asks for it (`BoardDef.update_scheme`, ADR 0015 decision 2) — the
nRF7002-DK does, in class A. The **bootloader** side is complete: its
`CONFIG_BOOT_SERIAL_BOOT_MODE=y` reads the area on every boot. The
**application** side is not: nothing in the framework calls
`bootmode_set()` yet, so `CONFIG_RETENTION_BOOT_MODE` stays off in the
application image and the devicetree node it would write to is simply
unused there. Applying the snippet to both images anyway costs nothing
(devicetree only) and is what makes enabling the application half a
Kconfig line rather than a re-bootstrap: the retention area is at the
address the already-installed bootloader reads. Until then, recovery is
entered with the board's button.
