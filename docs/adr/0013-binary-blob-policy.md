# 0013 — Binary blob policy, build profiles, and per-device Zephyr pinning

- Status: accepted
- Date: 2026-08-05

## Context

Three vendor components relevant to MCUHome exist only as precompiled
binaries ("blobs"):

- **Espressif WiFi/BLE stack** (hal_espressif) — without it, ESP32
  targets have no radio at all. There is no open alternative.
- **Nordic MPSL + SoftDevice Controller** (sdk-nrfxlib) — the only radio
  arbiter for *concurrent* BLE + 802.15.4 on nRF52/53/54. Required for
  Matter's standard smartphone/BLE commissioning flow (ADR 0011 path B);
  without it only on-network commissioning works.
- **Nordic nrf_cc3xx** (sdk-nrfxlib) — driver library for the CryptoCell
  CC312 on e.g. the nRF5340 application core: hardware TRNG plus
  accelerated AES/SHA/ECC. Prototype measurements showed software-only
  SPAKE2+ costs 2–3 s per PASE step on a plain Cortex-M33; hardware ECC
  reduces that to milliseconds. Also the only realistic secure entropy
  source on the nRF5340 app core besides a self-built netcore IPC seed
  service (see the entropy research, 2026-08-04).

The original "100 % open source, no proprietary code" stance therefore
cannot hold for the product MCUHome wants to be: a project a
non-technical user can adopt where commissioning "just works" from a
phone. End users care about outcomes (fast pairing, battery life,
standard compliance), not about how they are achieved.

Licensing: all three ship under vendor licenses that permit use on the
vendor's silicon. Distribution is handled Zephyr-style — users fetch
blobs from the vendor via `west blobs fetch`; MCUHome repositories and
images redistribute nothing.

## Decision (product owner, 2026-08-05)

1. **Blob policy: "no blobs where reasonably avoidable" with an explicit
   allow-list.** Allowed: Espressif WiFi/BLE, Nordic MPSL/SDC, Nordic
   nrf_cc3xx. Anything else needs a new PO decision.
2. **Two build profiles, chosen per device config:**
   - `standard` (default): uses the allow-listed blobs available for the
     target. Full feature set — BLE commissioning, hardware crypto.
   - `open`: no blobs at all. Fully auditable; functional consequences
     (on-network commissioning only on Nordic Thread targets, software
     crypto, no ESP32 WiFi) are documented, not hidden.
   The defaults serve the majority; the open profile is the deliberate
   opt-in for users who want it — not the other way around. Profile names
   are neutral (no "secure/insecure" framing). Both profiles are built in
   CI so neither rots.
3. **Conditional on feasibility.** MPSL/SDC and nrf_cc3xx integration on
   *vanilla* Zephyr is unproven (both expect NCS glue; nrf_cc3xx targets
   a different mbedTLS/PSA generation than our mbedTLS 4 stack). One
   analysis work package covers both (bundled with the ADR 0011 path-B
   feasibility work). If a blob cannot be sustained on vanilla Zephyr,
   the standard profile degrades to the open behaviour for that feature
   — the open path is always the working fallback.
4. **Resolution of the ADR 0008 tension — per-device Zephyr pinning
   instead of project-wide lag.** ADR 0008 (track latest stable) exists
   so new hardware is supported quickly; it stays. Vendor blobs are
   validated against specific Zephyr states, so:
   - The builder gains a core YAML option `zephyr_version` with default
     `auto`: the newest MCUHome-supported Zephyr that fully supports the
     device's board *and* profile. Users can pin explicitly or force
     `latest`.
   - Pins refer to a **release line** (e.g. `4.4`), never a frozen point
     release — patch releases with security backports are always taken.
   - **At most two Zephyr lines are maintained concurrently** (current
     latest + one blob-pin line); each maps to its own container image
     (ADR 0007) and patch series.
   - Validation UX: if a user forces a combination that cannot work
     (e.g. `zephyr_version: latest` + `blob_mode: standard` on a board
     whose blobs lag), the builder rejects it with a plain-language
     message that states MCUHome's recommendation (drop the pin — `auto`
     picks the best version) and the alternative (`blob_mode: open`),
     with a docs link. No raw technical detail in the error itself.

## Consequences

- ADR 0008 gets a cross-reference; its 6-monthly bump task now includes
  re-testing the blob glue and moving/retiring the pin line.
- The YAML schema gains `device.zephyr_version` (default `auto`) and
  `device.blob_mode` (`standard` | `open`, default `standard`); the
  builder's canonical device model carries the resolved Zephyr line and
  profile so the dashboard can display both.
- The v0.1 entropy plan is unchanged: netcore boot-seed + DRBG is built
  regardless, as the open-profile implementation and universal fallback;
  nrf_cc3xx becomes the standard-profile entropy/crypto provider once
  the feasibility analysis passes.
- Documentation states plainly which blobs each profile uses, from whom,
  and why — including that firmware built with the standard profile is
  not fully auditable.

## Amendment: per-blob granularity (2026-08-05, product owner)

Blob incompatibility with a Zephyr line is a per-blob property (MPSL may
work on latest while nrf_cc3xx lags), so an all-or-nothing profile
switch is the wrong resolution granularity. Refined model:

- Global `blob_mode: auto` (default; replaces the "standard" name) or
  `open` (all blobs off). Under `auto`, the builder resolves a per-blob
  structure for the board — only *applicable* blobs appear (no
  `espressif-wifi` entry on a Nordic board).
- Per-blob overrides in a `blobs:` map with three values:
  - `enabled` — hard requirement; version resolution must satisfy it,
    otherwise a validation error.
  - `disabled` — never used.
  - `auto` — **priority inversion**: the resolved Zephyr version wins,
    the blob is used iff compatible with it. This is the one-line answer
    the validator suggests when a user forces `zephyr_version` into
    conflict with a blob — and it self-heals: once the vendor ships a
    compatible blob for that line, it is picked up again automatically.
- Default semantics (no overrides): blobs are hard constraints and drive
  the automatic Zephyr pin (see the pinning rules above).
- Validation is peripheral-aware: each configured component is checked
  against driver availability in the resolved Zephyr line (the
  availability matrix is extracted automatically from the Zephyr trees
  when the builder container images are built, never hand-maintained).
  Errors state the conflict and both resolutions in plain language,
  including a copy-paste config snippet — but the builder NEVER flips
  functional trade-offs implicitly (adding a sensor must not silently
  remove BLE commissioning).
- **Recommendation-drift check:** on every build, explicit config
  entries are compared against what `auto` would currently produce;
  entries that have become redundant or now only restrict (e.g. a
  parked `nrf_cryptocell: auto` after the vendor caught up) produce an
  info-level hint recommending their removal.
