# 0015 — Update and partition architecture per board class

- Status: accepted
- Date: 2026-08-07

## Context

MCUHome devices have no bootloader today, and no enforced separation
between code and stored settings. Without MCUboot,
`USE_DT_CODE_PARTITION` stays off, so the linker hands the application
everything from the board's load offset to the end of flash. The
hardware-verified nRF7002-DK node and the phase-3 dongle build would
both grow straight over their own `storage_partition`, and the dongle
image over the factory bootloader as well. Nothing warns; the
image simply starts overwriting fabric credentials the day it gets big
enough. Every update also needs a debugger, which ADR 0016 rules out
as an end-user requirement.

**Sizing decides most of what follows.** Measured in phase-3 blocks
0-2 (host builds, Zephyr v4.4.0, CHIP v1.5.1.0 with the MCUHome patch
set, MCUboot `ee39e2d6` — the Zephyr-4.4 pin):

| Image | flash | RAM |
|---|---:|---:|
| `samples/matter-node`, nRF7002-DK | 551.5 KiB | 198.8 KiB |
| `samples/matter-node`, nRF52840 dongle | 562.9 KiB | 190.6 KiB (74.4 % of 256 KiB) |

A commissioned Matter node is ~560 KiB and almost none of it is
optional: `libCHIP.a` alone is 181.9 KiB, OpenThread FTD plus platform
and L2 another 132.3 KiB. **Two copies of that image do not fit in
1 MiB of internal flash.** That single fact removes every classic
MCUboot mode (swap, overwrite-only, direct-XIP, RAM-load) from any
1 MiB part — and admits them wherever the second copy can live
somewhere else. 1 MiB parts are not a corner case; they are the cheap
end of the hardware MCUHome wants to run on.

MCUboot's own cost, measured on the nRF7002-DK application core:

| MCUboot configuration | flash | RAM |
|---|---:|---:|
| single image, minimal | 23.3 KiB | 13.1 KiB |
| + CDC-ACM serial recovery | 51.3 KiB | 28.7 KiB |
| + retention boot-mode entrance | 52.2 KiB | 28.7 KiB |
| firmware-updater mode (loader *is* the recovery path) | 24.0 KiB | 13.0 KiB |

The same serial-recovery configuration on the nRF52840 dongle, from
MCUboot's own in-tree board file plus the `boot-mode` snippet:
**50.0 KiB**. The application side of the retention boot mode costs
+1,064 B flash and +72 B RAM.

The firmware-updater bootloader is less than half the size of the
serial-recovery one because it carries no USB stack, no SMP and no
zcbor — the loader application does. That trade (a small bootloader
plus a large loader partition, versus a large bootloader and no
loader partition) is the real choice on 1 MiB parts, and the numbers
below decide it per board class rather than globally.

Two further constraints frame the decision. ADR 0016 fixes the
assumption that end users have no SWD, so the recovery path must be
reachable over USB and must survive a failed update. And the
horizontal-scaling phase (more Nordic parts, ESP32, other MCUs) starts
once the vertical slice is complete, so anything written here as a
global constant becomes a re-opened ADR per new board.

## Decision

### 1. MCUboot on every target, integrated via sysbuild

Every MCUHome image boots through MCUboot. This is a choice, not a
default: on Espressif targets Zephyr's default is Simple Boot, and
MCUboot's Espressif port (esp32, c2, c3, c6, h2, s2, s3) is
first-class, so choosing MCUboot uniformly is what makes the
horizontal-scaling phase a matter of adding board rows rather than
adding a second update architecture. It also fixes the code/settings
overlap above as a side effect, on every board, immediately.

Vanilla Zephyr integrates MCUboot through **sysbuild only**. Builder
stage 5 therefore moves from a single-image `west build` to
`west build --sysbuild`, and the artifact set of
[builder-pipeline.md](../design/builder-pipeline.md) §7 grows from one
image to a per-image set (bootloader, signed application, and the
bootstrap artifact of ADR 0016). The manifest side is already
in place — `mcuboot` and `zcbor` are in the `west.yml` name-allowlist.

### 2. The update scheme is per-board registry data, never a global constant

`BoardDef` in `mcuhome/registry.py` gains the bootloader, update-scheme
and staging metadata as ordinary properties, next to the transports,
Kconfig and overlay it already carries: which MCUboot mode the board
uses, where the staging area lives (internal, external part, or none),
which recovery entrance it has, and the partition table that follows
from all three. Nothing in the builder may branch on a board name.

Consequence and intent: supporting a new board is a table row plus a
bring-up, exactly like adding a driver or a cluster. This ADR is
re-opened when a board needs a *scheme* that does not exist yet, not
when a board needs a different *size*.

### 3. Board class A — external staging flash present

Boards with a second flash part on board. Reference: the nRF7002-DK,
whose MX25R64 (8 MiB) sits on **SPI4, not QSPI** — QSPI carries the
nRF7002 Wi-Fi companion — so the driver is `CONFIG_SPI_NOR` and the
part cannot be executed from. Irrelevant for staging, which only reads,
writes and erases; relevant for anything that ever wants XIP. The
XIAO-class boards join this class later.

Scheme: **MCUboot swap, secondary slot in the external part**, which
is the only layout in reach that gives real rollback *and* satisfies
Matter OTA's expectations.

| Region | Where | Size |
|---|---|---:|
| boot (MCUboot) | internal `0x00000` | 64 KiB |
| slot0 (application) | internal `0x10000` | 928 KiB |
| storage (settings/NVS) | internal `0xF8000` | 32 KiB |
| slot1 (staging) | MX25R64 `0x00000` | 928 KiB |

928 KiB leaves 376.5 KiB of application headroom today, and the 8 MiB
external part makes the secondary slot free of internal-flash cost. The
64 KiB boot partition is the upstream default and holds 52.2 KiB in the
closest configuration measured (single image, serial recovery, boot
mode); the swap state machine sits on top of that and has not been
measured yet — the first thing the class-A bring-up reports.

`CONFIG_BOOT_MAX_IMG_SECTORS_AUTO=n` is set explicitly: MCUboot's
automatic sector-count derivation reads the block size of slot0's
flash node and applies it to slot1, which is wrong the moment the two
slots live on different parts (upstream bug candidate, to be filed).
The in-tree `bl5340_dvk` is the reference for an external secondary
slot; it uses QSPI, which is the one line that does not transfer.

CHIP's `OTAImageProcessorImpl` (Zephyr platform) cannot drive this
layout: it opens `DT_CHOSEN(zephyr_flash_controller)` — the internal
controller — and applies the `slot1_partition` offsets to it, so an
external secondary slot is unsupported as written. **MCUHome ships its
own image processor** in `components/matter/`, which is a framework
work item, not a patch.

### 4. Board class B — 1 MiB internal flash only

Reference: the nRF52840 dongle. Scheme: **single slot plus MCUboot
CDC-ACM serial recovery**, with the retention boot-mode entrance of the
committed `boot-mode` snippet. Layout in the MCUHome standard state
(ADR 0016 — vendor bootloader replaced, vendor MBR kept):

| Region | Size |
|---|---:|
| Nordic MBR | 4 KiB |
| boot (MCUboot + serial recovery + boot mode) | 56 KiB |
| slot0 (application) | 932 KiB |
| storage (settings/NVS) | 32 KiB |

Sizes, not addresses: MCUboot lands in the region the vendor bootloader
vacates, and the exact placement follows from where the MBR forwards the
reset vector — part of the open verification item in ADR 0016
decision 2. The measured bootloader is 50.0 KiB, so 56 KiB is the
partition with the growth it will see already in it.

Until a board is bootstrapped — and permanently, on any board whose
vendor bootloader cannot be replaced — the coexistence layout applies
instead: the Nordic MBR and the factory Open Bootloader's region leave
892 KiB usable, from which MCUboot takes 56 and storage 32, giving an
**804 KiB** application slot. That still clears the 562.9 KiB image by
241 KiB; it is a smaller budget, not a different architecture.

A USB-only firmware-updater variant would cost more, not less: a
24.0 KiB bootloader plus an `smp_svr` loader partition (82.0 KiB as
upstream ships it, 61.5 KiB trimmed of stats, taskstat, echo, log and
console) is ~35 KiB worse than the 50.0 KiB of MCUboot with recovery
built in, for the same USB-only user experience — and it gives up
serial recovery to get there.

**This class has no wireless full-image update in v0.x.** That is
physics, and it is stated as such rather than dressed up: 562.9 KiB
twice is 1,126 KiB in 1,024 KiB. Updates arrive over USB, through
serial recovery. A device in the field is updated by unplugging it.

The **documented growth path** for wireless updates on this class is
MCUboot's firmware-updater mode, and it is measured, not sketched:
a 24.0 KiB bootloader plus a 201.6 KiB Thread-capable non-Matter
loader (106.5 KiB RAM, never resident at the same time as the
application), leaving:

| Region | Size |
|---|---:|
| Nordic MBR | 4 KiB |
| boot (MCUboot, firmware-updater mode) | 24 KiB |
| loader slot (Thread-capable, non-Matter) | 202 KiB |
| slot0 (application) | 762 KiB |
| storage | 32 KiB |

762 KiB still clears today's image by 199 KiB. It is deliberately not
v0.x, for two reasons that are about risk rather than bytes: the mode
has **zero upstream tests and samples** (and does not even build
without an entrance mode selected explicitly — sysbuild wires none by
default), and a non-Matter loader cannot receive a Matter OTA, so the
scheme only closes once MCUHome owns a transfer protocol of its own.
It also gives up serial recovery, test/confirm and rollback, and
leaves updating the loader itself as the one unprotected write. When
that protocol exists (decision 6), this becomes a registry row change
plus one bootstrap re-flash.

### 5. Matter OTA lands on class A only, and not on a Kconfig flip

Measured cost on the nRF7002-DK in the class-A layout: **+27.0 KiB
flash, +2.0 KiB RAM** for the requestor compiled *and wired in*
(the DFU plumbing underneath it — MCUboot/img_manager/stream_flash/
SPI-NOR — is a further +1.3 KiB flash and +0 B RAM). Against a
551.5 KiB base that is 4.9 %, less than half the 50-90 KiB the
source-level estimate had suggested. This is the one part of the update
story that turned out cheap.

Enabling it is three work items, and `CONFIG_CHIP_OTA_REQUESTOR=y`
alone is none of them — the `=y` and `=n` builds were **byte-identical
at 566,100 B**, because `mcuhome-root.zap` has no OTA Software Update
Requestor cluster (0x002A) on endpoint 0, so CHIP never compiles the
cluster and `--gc-sections` drops what it does compile:

1. **The C10 patch into `patches/`**, and filed upstream.
   `src/platform/Zephyr/OTAImageProcessorImpl.cpp` uses
   `FIXED_PARTITION_OFFSET`/`FIXED_PARTITION_SIZE`, which Zephyr 4.4
   marks `__DEPRECATED_MACRO`, and CHIP's GN build compiles with
   `-Werror`. On our pin this is a **build breaker**, not a warning,
   and it applies even though decision 3 replaces the file's behaviour:
   CHIP's `BUILD.gn` compiles it whenever the requestor is enabled,
   regardless of what the application instantiates.
2. **Cluster 0x002A on EP0 in `components/matter/zap/mcuhome-root.zap`**,
   which is the framework's ZAP and therefore a per-release artifact,
   not per-device generation (ADR 0014 decision B).
3. **App-side requestor instantiation** in the framework, against
   MCUHome's own image processor.

All three are **gated** on first verifying, against the project's
reference network, the limitation Home Assistant documents: Matter
updates for Thread devices behind any Apple border router fail on mDNS
forwarding. Spending the work before knowing whether the delivery path
exists in that topology would be spending it blind. Matter's own spec
makes this cheap to defer — the OTA Requestor is an optional device
type (0x0012); a node without it is fully compliant.

### 6. USB/SMP is the mandatory baseline transport everywhere

Every board class gets the USB/SMP path, whatever else it gets. It is
the only transport that reaches an **uncommissioned** node: Matter OTA
requires a CASE session, fabric membership and administrator privilege,
so it structurally cannot be the first-flash path and cannot be the
unbrick path. Matter OTA and any future MCUHome transport are additions
on top, never replacements.

A transfer protocol of MCUHome's own (CoAP over Thread, which
OpenThread already provides) is **deferred to the maintenance channel
ADR 0010 reserved**, where it belongs together with script push and
diagnostics — one channel, designed once. Note for that design: CHIP
never verifies the Matter image digest on any platform, so MCUboot's
signature is the only payload trust anchor in the existing path too.
An update transport does not need cryptography of its own; it needs to
not weaken that anchor.

### 7. nRF5340 network core: image frozen in v0.x, updated via SWD

The network-core image (57.7 KiB of 256 KiB, the 802.15.4 serialization
image plus MCUHome's entropy endpoint) is frozen for v0.x and updated
with a debugger. Every v0.x nRF5340 device is a development kit with an
on-board debugger, so this costs nothing today. It is a conscious scope
decision, not an oversight: vanilla Zephyr has no network-core DFU path
at all — no `dfu_target`, no `dfu_multi_image`, no
`BOOT_IMAGE_ACCESS_HOOKS` implementation, and the application core
cannot even address the network core's NVMC (0x41080000 lies outside
its peripheral windows). CHIP's network-core OTA is NCS-only.

**The load-bearing assumption is stated explicitly: this choice is
reversible because every transition to a network-core bootloader scheme
requires one SWD flash anyway, and every v0.x device has a debugger.
That assumption expires with the first debugger-less nRF5340 product.**
At that point the pcd/b0n-equivalent port — shared-RAM staging plus
command protocol, a network-core-resident bootloader (the network-core
devicetree already defines boot and slot partitions upstream), and
application-side MCUboot image-access hooks — becomes a **planned work
item**, not a surprise.

Two invariants keep that door open:

- Application-core layouts do not consume every internal byte where
  avoidable, so a future network-core staging region stays carvable
  (moot on class A, binding on 1 MiB-only variants — see decision 9).
- The core-to-core IPC protocol stays **versioned and fail-loud**. The
  entropy service already is: `MCUHOME_ENTROPY_IPC_VERSION` rides in the
  low byte of every magic value, and a mismatched peer fails every
  request with `-EIO` instead of degrading quietly. A stale network-core
  image must never fail silently after an application-only update.

### 8. Signing keys, and no downgrade prevention in v0.x

**One real signing key per user.** Each MCUHome user is their own
firmware vendor: the builder generates an ECDSA P-256 key pair on first
use and stores it outside every repository and every build directory.
The key lives **where the user's controlling instance runs, never on a
build server** (PO refinement, 2026-08-07): the future dashboard
generates it on first need and keeps it in its own state (in a Home
Assistant add-on: a path of the shape `/config/mcuhome/signing.key`);
a user who already owns a key simply places or overwrites the file
there. This works because MCUboot signing is a detached post-build
step — `imgtool` over the finished binary — so a remote builder
returns an *unsigned* image and signing happens wherever the key is.
There is no central MCUHome signing key, no key in CI, and **MCUboot's
demo key is never used** — its private half is published in the
MCUboot tree, so signing with it verifies against a key the whole
world holds. That is theatre, and shipping it would be worse than
shipping nothing, because it looks like a signature.

Key rotation is a bootloader replacement, not a firmware update: the
public key is compiled into MCUboot, so rotating it means running the
ADR 0016 bootstrap operation again, through the same front door and
with physical access to the device. Rotation is therefore rare by
construction, which is an argument for generating the key well (once,
properly) rather than for rotating it often.

Custody of that key is structurally the same problem as custody of the
attestation root in ADR 0012 path B — a private key that must never
live in a repository or in CI, held by whoever operates the builder.
The two will want one answer. This ADR does not resolve path B; it
notes that the resolution must cover both.

**Downgrade prevention stays off in v0.x**
(`CONFIG_MCUBOOT_DOWNGRADE_PREVENTION=n`). Rolling a device back to a
known-good image is a normal act during development, and the v0.x
layouts set no readback protection, so an attacker with the board in
hand can erase and re-provision the part outright — downgrade
prevention would raise the cost of the developer's operation and not
the attacker's. Matter's own path is monotonic regardless: a provider
offers an image only for a higher `SoftwareVersion`. Revisit at 1.0,
together with readback protection.

### 9. Storage, reserved regions, and the version-number mapping

**32 KiB storage partition on every board.** The dongle's upstream
default is 16 KiB, which is smaller than the 32 KiB that
`CONFIG_SETTINGS_NVS_SECTOR_COUNT=8` needs; CHIP's own dongle layout
uses 32 KiB. Matter fabric credentials and the Thread dataset live
there, and **every layout in this ADR preserves it across updates** —
it is a separate fixed partition in all of them, which is what makes an
update not a re-commissioning.

**Reserved regions are named in the layout tables, not squeezed in
later.** Two are known to be coming and neither has a format yet: a
script/data area for the scripting phase
([component-model.md](../design/component-model.md) §10) and, on 1 MiB
nRF5340 variants, a network-core staging region (decision 7). Both are
carved from the top of the application slot, adjacent to `storage`, so
that instantiating one moves exactly one boundary and nothing else. In
v0.x their size is zero. The script area is explicitly **not**
MCUboot-image-framed: `IMAGE_F_NON_BOOTABLE` is a trap (swap loaders
scramble the trailer), and an extra updateable image conflicts with the
seconds-level push, staged apply and last-good fallback that
component-model.md §10 asks for. Reservation only; the format is
decided in the scripting phase.

**SemVer to Matter `SoftwareVersion` (u32):**
`major << 24 | minor << 16 | patch << 8`, matching CHIP's own
`ota-image.cmake` convention, with the low byte reserved for a tweak
counter. `SoftwareVersionString` is the SemVer string verbatim. This
belongs here because ADR 0005 fixes SemVer as the project's version
scheme while Matter requires a monotonically comparable number, and the
mapping has to be fixed before the first image is published, not after.

## Consequences

- Builder stage 5 becomes a sysbuild build. Artifacts, the memory
  report and the build manifest all become per-image; `--generate-only`
  is unaffected. This is the largest single piece of implementation
  work this ADR creates.
- Every board brought up from here on carries a partition table and a
  scheme in `BoardDef`, and neither can be inherited from an upstream
  board default — the upstream dongle table (two 408 KiB slots, 16 KiB
  storage) cannot hold an MCUHome image at all.
- Class A devices can roll back a failed update; class B devices
  cannot, and their recovery is a USB cable. Documentation must say so
  in those words, next to the board list, before anyone chooses hardware
  on the strength of it.
- The framework gains an OTA image processor of its own, and the patch
  set gains the C10 hunk — both only when decision 5's gate opens.
- Existing devices are re-flashed once when they enter the standard
  state — the phase-1 node has no bootloader today and its application
  starts at `0x0`. `storage_partition` keeps the address and size it
  already has upstream on the nRF5340 application core (`0xF8000`,
  32 KiB), so credentials survive the move unless the part is
  mass-erased. No re-commissioning follows from this ADR; the one in
  ADR 0014 is unrelated and already paid.
- Related standing decisions: ADR 0007 (nothing here adds a host tool —
  the bootloader and signing happen inside the builder image), ADR 0008
  (the bootloader's Zephyr line is pinned separately from the
  application's; see ADR 0016 decision 4), ADR 0010 (the reserved
  maintenance channel is where an MCUHome transport lands), ADR 0012
  (shared key-custody question), ADR 0014 (the ZAP that gains cluster
  0x002A is the framework's, not the device's), and ADR 0016, which
  fixes how a board reaches the state this ADR partitions.

## Amendment: class-A boot partition 80 KiB + bootloader size levers (2026-08-07, product owner)

Decision 3's own text called this out: "there is no comfortable margin
left in this partition; the next feature that lands in the bootloader
moves the boundary at `slot0_partition`, and moving it is a re-bootstrap
of every device already in the field." That prompted a size audit before
anything else landed in the bootloader.

**Measured, 2026-08-07:**

| Question | Answer |
|---|---:|
| How full is the original 64 KiB partition? | 63.1 KiB — **98.6 %** |
| Does `-Oz` (aggressive size) beat the `-Os` the build already uses? | No — byte-identical image, a null lever |
| Cost of link-time optimization, this image only (`CONFIG_LTO=y`, `CONFIG_LTO_SINGLE_THREADED=y`, `CONFIG_ISR_TABLES_LOCAL_DECLARATION=y`) | **-7.55 KiB** |
| Cost of dropping the dead UART driver `MCUBOOT_SERIAL` links in regardless of the selected serial-recovery transport (upstream imprecision, workspace `UPSTREAM-BUGS.md` entry M2), via `&uart0 { status = "disabled"; };` in the bootloader-only overlay | **-1.30 KiB** |
| Combined | **55.4 KiB** |

Both levers are real, orthogonal savings, and combined they are enough
on their own to clear a 15 %-free bar at the *original* 64 KiB
partition — no partition growth required. Shipping them and leaving the
partition where it was would have been a defensible, cheaper close to
the size question decision 3 opened.

**The product owner's call is to grow the boot partition to 80 KiB
anyway**, and take both levers on top of that. The argument is headroom,
not need: 80 KiB at 55.4 KiB measured is 69 % full, with 24.6 KiB free
for whatever lands in the bootloader next (a signature scheme change, a
second recovery transport, anything decision 8 revisits at 1.0) — well
past what either partition size needed to clear the 15 %-free bar. That
headroom is worth more than reclaiming 16 KiB into `slot0_partition`,
where it would buy one more percent of an application slot that is
nowhere near full.

Crucially, this decision **predates any bootstrapped device** — v0.x has
shipped no board through the ADR 0016 standard-state procedure yet, so
unlike the "next feature is a re-bootstrap" framing decision 3 used to
argue for restraint, moving this boundary now re-bootstraps nothing. It
costs exactly the 16 KiB of `slot0_partition` it takes and nothing else.
That asymmetry — cheap today, a field re-bootstrap after the first
device ships — is also the argument for taking the headroom now rather
than deferring the question to the next feature that actually needs it.

The class-A layout of decision 3, as amended:

| Region | Where | Size |
|---|---|---:|
| boot (MCUboot, LTO + dead-UART levers) | internal `0x00000` | 80 KiB |
| slot0 (application) | internal `0x14000` | 912 KiB |
| storage (settings/NVS) | internal `0xF8000` | 32 KiB |
| slot1 (staging) | MX25R64 `0x00000` | 912 KiB |

`slot0`/`slot1` shrink together, by construction (decision 3's own
"swap needs one sector layout across both slots" — the two stay equal
by definition, not by a second measurement); `storage_partition` is
untouched, so this amendment carries none of decision 3's
re-commissioning risk. `mcuhome/registry.py`'s `PartitionDef` sizes, the
`nrf7002dk/nrf5340/cpuapp` partition overlay (now restating
`boot_partition` too, since 80 KiB is no longer the board's upstream
default the way 64 KiB was) and `CONFIG_BOOT_MAX_IMG_SECTORS` carry these
numbers; both size levers are registry data on the same scheme, per
decision 2.

## Amendment: fault-driven health monitoring and automatic rollback (2026-08-07, product owner)

Recorded now, implemented with the update/OTA block — deliberately
written down so neither half can be forgotten:

**Mandatory for every application image:** fatal errors REBOOT, never
halt (`CONFIG_RESET_ON_FATAL_ERROR=y`), and the hardware watchdog is
enabled and fed by the application. A device built into a wall cannot
be power-cycled by hand; a board hanging in a fault state is never
acceptable, and only a reboot lets MCUboot's revert machinery act.

On top of MCUboot's native test/confirm revert (an image that faults
before confirming is swapped back automatically on the next boot), the
product owner specified a fault-counting model for failures that
appear *after* confirmation:

- Two persistent counters, both filtered by **reset cause** (hwinfo):
  only fault-class resets count — watchdog and fatal-error reboots.
  Power-on, brownout and pin resets never increment anything ("boot
  counter" would be the wrong name; these are fault-reset counters).
- The new image confirms itself to MCUboot after ~30 s of healthy
  uptime.
- A long-run counter increments once a boot exceeds a long-run
  threshold of uptime (the PO sketch named 10 and 30 minutes in
  different places; the exact values are fixed as named configurable
  constants at implementation).
- **If fault-resets > 5 while long-runs ≤ 2, the application marks
  itself bad** and requests the swap back to the previous image —
  which still sits in the staging slot until a newer download
  replaces it. An image can do this actively at any time; before
  confirmation, simply not confirming plus a reboot is sufficient.
- Rationale: a device that keeps faulting without accumulating real
  uptime is unreachable over the air — automatic revert is the only
  way out. A fault that only appears after long uptime leaves ample
  time to deliver a fixed image via OTA and must not trigger revert.

Class-B note: single-slot boards have no previous image to revert to;
what the counters do there (at most: drop to serial recovery) is
specified at implementation.
