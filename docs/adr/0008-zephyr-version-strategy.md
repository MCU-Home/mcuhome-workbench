# 0008 — Zephyr version strategy: track latest stable, not LTS

- Status: accepted
- Date: 2026-08-02
- Finalized: 2026-08-14

## Context

Zephyr releases every ~6 months; an LTS appears only every ~2.5–3 years
(3.7 in mid-2024, next: 4.6 expected ~April 2027) and receives only bug
and security fixes afterwards — no new SoC support, no new features.
Support for exactly the chips MCUHome targets (nRF54 series, ESP32-C6/H2 —
the low-power Thread-capable parts) matures in current releases, and the
Matter SDK is developed and version-paired against current Zephyr. The
classic LTS argument (upgrade friction) barely applies to MCUHome: users
and contributors consume pinned revisions via `west.yml` plus a
build-container image versioned in lockstep with that pin (ADR 0007), so
Zephyr bumps are invisible to them.

One force pulls the other way: vendor binary blobs
([draft ADR 0013](draft/0013-binary-blob-policy.md)) are validated
against specific Zephyr states and may temporarily be incompatible with
the latest stable line. The decision below absorbs that per device, not
project-wide.

## Decision

- Pin the **latest stable Zephyr release** in `west.yml` (currently
  v4.4.0); never track `main`.
- What MCUHome supports is a release **line**, never a frozen point
  release: `west.yml` pins the exact release the project itself builds
  and CI verifies, but a build container carrying any release *within* a
  supported line serves that line, so patch releases with security
  backports are always taken without any document changing. The
  supported lines are enumerated in one place
  (`mcuhome.model.toolchain.SUPPORTED_ZEPHYR_LINES`), and the line is
  the vocabulary the build machinery speaks: a build context requires a
  Zephyr line and a backend matches it against the image's
  `org.mcuhome.zephyr` label (contract §3.2, ADR 0018).
- Bump deliberately once per Zephyr release cycle (~every 6 months) as a
  dedicated, tested maintenance task, together with the matching
  build-container image and the Matter SDK pin — CHIP and Zephyr move as
  a matched pair (ADR 0006; currently CHIP v1.5.1.0 against Zephyr
  v4.4.0). Later decisions have grown the bump task's checklist:
  re-test blob glue and move or retire per-device pin lines
  ([draft ADR 0013](draft/0013-binary-blob-policy.md)), and re-check
  whether MCUboot's `serial_adapter` has been ported to the device-next
  USB stack, unpinning the bootloader if so
  ([draft ADR 0016](draft/0016-device-onboarding-and-flash-transport.md)).
- No dual-track (latest + LTS backports) — unrealistic at current team
  size.
- **Blob incompatibility is handled per device, not by lagging the
  project.** Where a required blob is not yet validated against the
  latest line, the affected device configuration pins a Zephyr release
  line (`device.zephyr_version`, default `auto`), with at most two lines
  maintained concurrently — today there is exactly one. The mechanism is
  [draft ADR 0013](draft/0013-binary-blob-policy.md)'s; the tracking
  strategy above is unchanged by it. The one standing per-image
  exception is the bootloader, pinned to the 4.4 line independently of
  the application for the recovery-transport reason
  [draft ADR 0016](draft/0016-device-onboarding-and-flash-transport.md)
  records; the application follows this ADR's cadence unchanged.
- **Re-evaluate when Zephyr 4.6 LTS ships (~April 2027):** decide then
  whether MCUHome 1.0 should land on the LTS baseline. Supersede this ADR
  explicitly if so.

## Consequences

- Full access to new SoC support and current Matter/OpenThread work.
- MCUHome absorbs upstream churn itself, at a controlled 6-month cadence;
  regressions surface during the bump task, not at user machines.
- Each MCUHome release documents its exact Zephyr/SDK pairing via
  `west.yml` and the build-container image tag
  (`zephyr-<release>-r<revision>`, ADR 0007); a test asserts the tag
  against the manifest's pin, so bumping one without the other fails
  loudly.
- Because compatibility is expressed in lines rather than exact
  releases, a security respin of Zephyr or of a build container within
  the supported line is picked up without republishing or re-pinning
  anything.
