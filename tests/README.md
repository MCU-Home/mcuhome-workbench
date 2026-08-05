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

CI (`.github/workflows/ci.yml`) landed together with this first suite, per
repo policy (we do not ship a red pipeline). It currently only runs the
lint/licensing checks `.pre-commit-config.yaml` runs locally; the twister
build itself is not wired into CI yet (needs a full west workspace — see
the TODO block in the workflow file).
