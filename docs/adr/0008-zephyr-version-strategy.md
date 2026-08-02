# 0008 — Zephyr version strategy: track latest stable, not LTS

- Status: accepted
- Date: 2026-08-02

## Context

Zephyr releases every ~6 months; an LTS appears only every ~2.5–3 years
(3.7 in mid-2024, next: 4.6 expected ~April 2027) and receives only bug
and security fixes afterwards — no new SoC support, no new features.
Support for exactly the chips MCUHome targets (nRF54 series, ESP32-C6/H2 —
the low-power Thread-capable parts) matures in current releases, and the
Matter SDK is developed and version-paired against current Zephyr. The
classic LTS argument (upgrade friction) barely applies to MCUHome: users
and contributors consume pinned revisions via `west.yml` plus a versioned
toolchain container (ADR 0007), so Zephyr bumps are invisible to them.

## Decision

- Pin the **latest stable Zephyr release** in `west.yml` (currently
  v4.4.0); never track `main`.
- Bump deliberately once per Zephyr release cycle (~every 6 months) as a
  dedicated, tested maintenance task, together with the matching container
  image and (once present) the Matter SDK pin.
- No dual-track (latest + LTS backports) — unrealistic at current team
  size.
- **Re-evaluate when Zephyr 4.6 LTS ships (~April 2027):** decide then
  whether MCUHome 1.0 should land on the LTS baseline. Supersede this ADR
  explicitly if so.

## Consequences

- Full access to new SoC support and current Matter/OpenThread work.
- MCUHome absorbs upstream churn itself, at a controlled 6-month cadence;
  regressions surface during the bump task, not at user machines.
- Each MCUHome release documents its exact Zephyr/SDK pairing via
  `west.yml` and the container image tag.
