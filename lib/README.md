# lib/

Portable libraries of the MCUHome runtime (no direct hardware access;
testable on `native_sim`).

Current libraries:

| Library | Purpose |
|---|---|
| `debug/` | `MCUHOME_RTT_REINIT`: explicit `SEGGER_RTT_Init()` at boot for log-only RTT builds (stale-control-block fix, upstream candidate Z7). Deliberately hardware-adjacent — the no-hardware/native_sim rule below is the target for future libraries, not a description of this one. |

