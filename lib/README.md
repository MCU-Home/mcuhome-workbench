# lib/

Portable libraries of the MCUHome runtime (no direct hardware access;
testable on `native_sim`).

Current libraries:

| Library | Purpose |
|---|---|
| `health/` | `MCUHOME_HEALTH`: the three mechanisms an updatable device cannot go without (ADR 0015 health amendment) — a fatal error reboots instead of halting, a hardware watchdog fed from registered liveness slots rather than from a timer, and image self-confirmation after a healthy delay so MCUboot's revert path stays armed until then. Public API: `<mcuhome/health.h>`. Hardware-adjacent for the same reason as `debug/` below. |
| `debug/` | `MCUHOME_RTT_REINIT`: explicit `SEGGER_RTT_Init()` at boot for log-only RTT builds (stale-control-block fix, upstream candidate Z7). Deliberately hardware-adjacent — the no-hardware/native_sim rule below is the target for future libraries, not a description of this one. |

