# tests/

Twister test suites (`testcase.yaml` per suite). Run from the workspace
top directory with:

```sh
west twister -T mcuhome/tests --integration --inline-logs -v
```

Host-run unit tests target `native_sim` — plain `native_sim` needs a 32-bit
host glibc that not every dev machine has, so suites here target the 64-bit
`native_sim/native/64` variant instead.

| Suite | Covers |
|---|---|
| `matter_tables/` | `components/matter/src/table_validate.c` — the CHIP-free tables-contract validator (ADR 0014). CHIP cannot build on native_sim, which is why validation lives in its own translation unit, separate from the CHIP-coupled `endpoint_registry.cpp` that calls it. |
| `channel/` | `components/sensor/src/sensor_convert.c` — the channel layer's unit conversion, rounding, saturation and report-on-delta decision. Same pattern: the arithmetic is a Zephyr-free, CHIP-free translation unit precisely so it can be exercised exhaustively on the host, while the poller around it (devices, workqueue, Matter reporting) stays on target. |
| `ota_image_header/` | `components/matter/src/ota_image_header.c` — the Matter OTA image-header parser (ADR 0015 decision 5). Same pattern again, and for a sharper reason: this parser decides what gets written into the other half of the device's flash, so it is a CHIP-free, Zephyr-free, heap-free translation unit that can be fed malformed input exhaustively on the host. Two of its inputs are golden headers captured byte for byte from CHIP's own `ota_image_tool.py`, which is the tool the builder drives; the rest are hand-built truncations, wrong magics and lengths that run past the end. |
| `ota_staging/` | `components/matter/src/ota_staging.c` — **where** in the staging slot a downloaded image lands. MCUboot's swap-using-offset mode reads the update's header one erase sector into the secondary slot, and an image written at its start is refused silently: the bootloader logs one line, and the shipping configuration used to compile that line out. This suite writes through the real code, reads the slot back through the flash map, and asserts the position — against an expectation derived from the devicetree and the bootloader mode, never from the code under test. Two scenarios, because a writer that simply always added a sector would pass a single one: `swap_using_offset` (what board class A runs) and `swap_using_move` (no offset). native_sim's simulated flash supplies the slot; the suite's own `Kconfig` explains the one symbol it has to force to get there. |
| `entropy_ipc/` | `drivers/entropy/entropy_ipc_core.c` — the CTR-DRBG and the seeded/unseeded/reseeding state machine of the netcore entropy driver, plus the wire framing in `include/mcuhome/entropy_ipc.h`. The transport is faked through `struct mcuhome_entropy_seed_source`, which is what makes the seed timeout, the retry behaviour and the reseed interval testable in seconds instead of on a two-core hardware cycle. |

CI (`.github/workflows/ci.yml`) landed together with this first suite, per
repo policy (we do not ship a red pipeline). It currently only runs the
lint/licensing checks `.pre-commit-config.yaml` runs locally; the twister
build itself is not wired into CI yet (needs a full west workspace — see
the TODO block in the workflow file).
