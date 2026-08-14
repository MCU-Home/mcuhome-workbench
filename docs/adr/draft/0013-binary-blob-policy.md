# 0013 — Binary blob policy, build profiles, and per-device Zephyr pinning

- Status: draft
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
  source on the nRF5340 app core besides the netcore IPC seed service
  MCUHome ships (entropy research 2026-08-04; see Consequences).

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
   nrf_cc3xx. Anything else needs a new PO decision. The same policy
   covers proprietary vendor **host tools**: one may appear as a
   documented one-time, vendor-side user step (modern `nrfutil` is a
   proprietary Nordic binary — the case ADR 0016 handles for dongle
   onboarding), but it is never a project dependency — no script,
   Makefile target, workflow or documented contributor step may require
   or invoke it.
2. **Blob use is a per-device, per-blob configuration.** The two
   resulting build states are the "build profiles" of the title, but
   they are selected by one key with per-blob overrides, not by named
   all-or-nothing modes (the key was renamed from `blob_mode`, its
   `none` value from `open`, 2026-08-05) — blob incompatibility with a Zephyr line is a
   per-blob property (MPSL may work on latest while nrf_cc3xx lags), so
   an all-or-nothing switch would be the wrong resolution granularity:
   - `device.blob_usage: auto` (default): the allow-listed blobs
     applicable to the board are used. Full feature set — hardware
     crypto, and BLE commissioning once it exists (itself gated behind
     the ADR 0011 path-B feasibility work and off in v0.1). Under
     `auto` the resolution is a per-blob structure for the board — only
     *applicable* blobs appear (no `espressif-wifi` entry on a Nordic
     board).
   - `device.blob_usage: none`: no blobs at all. Fully auditable; the
     functional consequences (on-network commissioning only on Nordic
     Thread targets, software crypto, no ESP32 WiFi) are documented,
     not hidden.
   - Per-blob overrides in a `blobs:` map with three values:
     - `enabled` — hard requirement; version resolution must satisfy
       it, otherwise a validation error.
     - `disabled` — never used.
     - `auto` — **priority inversion**: the resolved Zephyr version
       wins, the blob is used iff compatible with it. This is the
       one-line answer the validator suggests when a user forces
       `zephyr_version` into conflict with a blob — and it self-heals:
       once the vendor ships a compatible blob for that line, it is
       picked up again automatically.
   - Default semantics (no overrides): blobs are hard constraints and
     drive the automatic Zephyr pin (decision 4).
   - The value names are neutral and describe the effect, not the
     motivation — no "secure/insecure" framing, no "standard"/"open"
     mode names (documentation may still call the `none` result the
     "fully open build"). The defaults serve the majority; the
     blob-free build is the deliberate opt-in for users who want it —
     not the other way around. From the first integrated blob on, both
     configurations are built in CI so neither rots (until then the
     two are byte-identical, so today's one CI build covers both).
3. **Conditional on feasibility.** MPSL/SDC and nrf_cc3xx integration on
   *vanilla* Zephyr is unproven (both expect NCS glue; nrf_cc3xx targets
   a different mbedTLS/PSA generation than our mbedTLS 4 stack). One
   analysis work package covers both (bundled with the ADR 0011 path-B
   feasibility work). If a blob cannot be sustained on vanilla Zephyr,
   `blob_usage: auto` degrades to the blob-free behaviour for that
   feature — the blob-free path is always the working fallback.
4. **Resolution of the ADR 0008 tension — per-device Zephyr pinning
   instead of project-wide lag.** ADR 0008 (track latest stable) exists
   so new hardware is supported quickly; it stays. Vendor blobs are
   validated against specific Zephyr states, so:
   - The device configuration carries a core YAML option
     `device.zephyr_version` with default `auto`: the newest
     MCUHome-supported Zephyr that fully supports the device's board
     *and* blob configuration. Users can pin an explicit line or force
     `latest` (the newest supported line regardless of what blobs it
     costs).
   - A pin names a **release line** (e.g. `4.4`) — a line, never a
     frozen point release: patch releases with security backports are
     always taken. The line is the vocabulary the whole build machinery
     speaks: the supported lines are enumerated in one place
     (`mcuhome.model.toolchain.SUPPORTED_ZEPHYR_LINES`), the canonical
     device model carries the resolved line as `toolchain.zephyr_line`,
     a build context *requires* that line (its `zephyr:` field, context
     format 2 — ADR 0018, decision E61), and a backend matches the
     requirement against a build-container image's `org.mcuhome.zephyr`
     label (build-container-contract.md §2.1.1, §3.2): any release
     *within* the line serves the line; a suffixed pre-release serves
     none. (This Zephyr pin is distinct from the SDK version
     constraint, which is PEP 440 — ADR 0018 decision 3; an early draft
     of that ADR conflated the two.)
   - **At most two Zephyr lines are maintained concurrently** (current
     latest + one blob-pin line); each is served by its own
     build-container image and patch series (ADR 0007, ADR 0019).
     Today exactly one line exists: `4.4`.
   - Validation UX: if a user forces a combination that cannot work
     (e.g. `zephyr_version: latest` while a required blob's support
     lags), validation rejects it with a plain-language message that
     states MCUHome's recommendation (drop the pin — `auto` picks the
     best version) and the alternative (`blob_usage: none`, or the
     per-blob `auto`), with a docs link. No raw technical detail in the
     error itself.

### Validation and resolution semantics

- Validation is peripheral-aware: each configured component is checked
  against driver availability in the resolved Zephyr line. The
  availability matrix is extracted automatically from the Zephyr trees
  when the build-container images are built, never hand-maintained.
- Errors state the conflict and both resolutions in plain language,
  including a copy-paste config snippet — but the build pipeline NEVER
  flips functional trade-offs implicitly (adding a sensor must not
  silently remove BLE commissioning).
- **Recommendation-drift check:** on every build, explicit config
  entries are compared against what `auto` would currently produce;
  entries that have become redundant or now only restrict (e.g. a
  parked `nordic-cc3xx: auto` after the vendor caught up) produce an
  info-level hint recommending their removal.

## What exists today: the schema and the seam, not the machinery

v0.1 implements the vocabulary and the hook, staged deliberately
(product owner, 2026-08-07). All keys above are accepted and validated
(`device.blob_usage`, `device.zephyr_version`, `device.blobs` —
yaml-schema.md §3), but resolution is trivial, because exactly one
Zephyr line exists and no blob is integrated yet:

- The seam is `mcuhome/model/toolchain.py` in the `mcuhome-model`
  distribution (ADR 0020): `SUPPORTED_ZEPHYR_LINES` — `("4.4",)` today
  — is the single place the two-line rule will grow into, and
  `available_blobs()` is the single hook the availability matrix plugs
  into; it returns nothing for every board.
- `blobs: <name>: enabled` produces the plain-language refusal decision
  2 requires, naming this ADR (pinned by `tests_py/test_validate.py`);
  `disabled` and `auto` are accepted as honest no-ops — nothing is
  being switched off, and `auto` is defined to self-heal.
- An unsupported `zephyr_version` is refused with the recommendation to
  drop the pin; `auto` and `latest` both resolve to the one line.
- The line-matching half of decision 4 is already real and in
  production use, not staged: `satisfies_line`, `line_of` and
  `normalize_release` in the same module are what both the local build
  method and the build server use to match a context's required line
  against image labels (E61).

The full resolution machinery — the automatically extracted
availability matrix, per-blob resolution, the recommendation-drift
check, two-line maintenance — is built when it first has real inputs:
the first integrated blob or the second supported Zephyr line,
whichever comes first (expected with the MPSL/cc3xx feasibility
analysis, ADR 0011 path B).

## Consequences

- ADR 0008 resolves its blob tension with this mechanism and says so;
  its ~6-monthly Zephyr bump task includes re-testing the blob glue and
  moving/retiring the per-device pin line.
- The YAML schema carries `device.zephyr_version` (default `auto`),
  `device.blob_usage` (`auto` | `none`, default `auto`) and the
  per-blob `device.blobs` map; the canonical device model carries the
  resolved line (`toolchain.zephyr_line`) and the blob decisions so the
  dashboard can display both — and the resolved line is exactly what a
  build context states as its Zephyr requirement (ADR 0018, ADR 0019).
- The entropy path this ADR presupposed is built and shipping: the
  netcore boot-seed + CTR-DRBG driver (`drivers/entropy/`) is the
  `blob_usage: none` implementation and the universal fallback;
  nrf_cc3xx becomes the `blob_usage: auto` entropy/crypto provider once
  the decision-3 analysis passes — it replaces the driver's seed-source
  structure (`struct mcuhome_entropy_seed_source`) and nothing else.
- Documentation states plainly which blobs each configuration uses,
  from whom, and why — including that firmware built with
  `blob_usage: auto` on a board with applicable blobs is not fully
  auditable.
