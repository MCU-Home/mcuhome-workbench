# 0016 — Device onboarding, the MCUHome standard state, and flash transport

- Status: accepted
- Date: 2026-08-07

## Context

The bar MCUHome is measured against is ESPHome's: plug a board into a
USB port and flash it from a browser tab. Nothing in that story
involves a debug probe, and the audience MCUHome is for does not own
one. Every path designed on the assumption that SWD is available is a
path that works for the author and nobody else.

The boards themselves already solve the first flash — each ships with
a vendor mechanism that survives whatever the user does next:

- **nRF52840 dongle:** Nordic's Open Bootloader, DFU over USB, no probe
  and no SWD header on the board at all.
- **XIAO-class boards:** the Adafruit UF2 bootloader — a mass-storage
  device that takes a dragged file, on every operating system, with no
  tooling whatsoever.
- **Development kits** (nRF7002-DK, nRF52840-DK): an on-board J-Link,
  because they are development kits.

What those mechanisms cost is flash. The dongle's Open Bootloader
occupies 112 KiB; a UF2 bootloader with its SoftDevice baggage occupies
around 204 KiB. MCUboot with CDC-ACM serial recovery and the retention
boot-mode entrance measures **50.0 KiB** on the dongle. Keeping the
vendor bootloader *and* adding MCUboot on top — the chain Zephyr's own
board documentation describes, MCUboot installed at `0x1000` as the
Open Bootloader's application — therefore spends 162 KiB of a 1 MiB
part on two bootloaders that do the same job. The headroom matters: the
Matter OTA requestor measured +27.0 KiB, and the scripting VM, further
clusters and eventually BLE are all still to come.

The transport side has one hard external date. MCUboot's serial
recovery speaks SMP over a CDC-ACM port, and its `serial_adapter` uses
Zephyr's **legacy** USB stack, with no device-next support at all.
Legacy USB is deprecated with removal stated for Zephyr **v4.5
(~2026-10)** — which is precisely the next bump on ADR 0008's cadence.

ADR 0007 already anticipated the direction ("device flashing will
prefer browser-based flashing … details are a design-phase topic").
This is that design phase, and its constraint is ADR 0007's own: a
compiling, flashable MCUHome must not grow host prerequisites beyond
git and docker.

## Decision

### 1. No SWD is the design assumption

Every onboarding path is designed for a user who has no debug probe and
never will. The **first** flash of any board uses the manufacturer's
shipped path — Nordic Open Bootloader DFU on the dongle, UF2
drag-and-drop on XIAO-class boards, J-Link on a development kit because
it is a development kit and already has one. MCUHome neither requires
nor ships a probe-based path for end users.

### 2. The MCUHome standard state, reached by front-door bootloader replacement

**One board-specific bootstrap step** brings any supported board into
the **MCUHome standard state**:

> the vendor MBR is kept where one exists (4 KiB on Nordic parts), the
> vendor bootloader is **replaced** by MCUHome's MCUboot, with CDC-ACM
> serial recovery as the permanent, debugger-free rescue path.

After bootstrap every board behaves identically — same recovery entry,
same transport, same signing, same partition rules (ADR 0015). The
bootstrap step is run once per board, ever.

**Mechanism of record: front-door replacement.** MCUboot is packaged as
a *bootloader update* in the vendor's own format and installed through
the vendor's own mechanism. Nordic's Open Bootloader accepts
bootloader-update DFU packages and hands the staged image to the MBR,
whose copy command is persisted and resumed after a reset, so the
replacement is atomic and power-loss-safe. Adafruit/UF2 bootloaders
accept bootloader-update UF2s the same way.

**MCUHome never writes the boot region from application context.**
That is the failure mode that turns a supported board into
electronic waste in a user's hand, and no amount of care makes an
application-context boot-region write safe on a part with no recovery
path.

What it buys: the dongle's 112 KiB of vendor bootloader collapse into
~56 KiB of MCUboot, and the second copy of MCUboot that coexistence
needs at `0x1000` disappears with it — in ADR 0015's layouts, an
application slot of **804 KiB in coexistence against 932 KiB in the
standard state**. On UF2 boards the same step replaces ~204 KiB
(bootloader plus SoftDevice baggage) with the same ~56 KiB.

**Coexistence mode is the fallback**, not the plan: boards whose vendor
bootloader is locked or cannot update itself keep it, and MCUboot is
installed as its application — the chain upstream documents, and the one
phase 3 built and measured — at the known flash cost. Both modes are
supported states; only the standard state is the target.

**Registry property.** The bootstrap path — mechanism, artifact format
and the first-flash instructions generated for the user — is a
per-board `BoardDef` property alongside the update scheme of ADR 0015
decision 2. A new board declares how it is bootstrapped; it does not
add a code path.

**Open verification item, phase-3 prototype block:** whether the stock
Open Bootloader accepts an *unsigned* bootloader-update package, and
what the MBR's staged-copy semantics do under power loss, on real
hardware. The layout in ADR 0015 decision 4 assumes MCUboot ends up at
the address the MBR forwards the reset vector to. If the verification
fails, the dongle stays in coexistence mode — a smaller application
slot, not a different architecture.

### 3. Buttonless recovery entry

Recovery is entered without touching the board: the application writes
the retention boot mode (`bootmode_set()`, the committed `boot-mode`
snippet's GPREGRET1 area) and reboots, and MCUboot reads it via
`CONFIG_BOOT_SERIAL_BOOT_MODE`. Remotely the same request arrives as an
SMP reset carrying `boot_mode 1`
(`CONFIG_MCUMGR_GRP_OS_RESET_BOOT_MODE`). Where a board has a button,
the physical entrance stays enabled as the escape hatch — it is the
only path left when the application no longer boots far enough to write
a register.

### 4. The bootloader is pinned to Zephyr 4.4, independently of the application

Serial recovery's dependency on the legacy USB stack has a removal date
(Context). The mitigation is to accept it rather than to design around
it: **the bootloader image is pinned to the Zephyr 4.4 line
independently of the application's Zephyr version.** Bootloaders are
frozen by nature — a device's MCUboot is written once, at bootstrap,
and the recovery *protocol* (SMP over serial) is the stable interface,
not the USB driver underneath it. The application follows ADR 0008's
cadence unchanged.

Porting MCUboot's `serial_adapter` to device-next is noted as an
**upstream-contribution candidate**, alongside the patch set's existing
upstream candidates. It is the clean end state; until someone does it,
per-image pinning is what keeps the recovery path alive across the v4.5
bump.

### 5. Browser flashing over Web Serial, with our own SMP client

**Web Serial is the API.** Firefox 151 shipped it (May 2026), joining
Chrome, Edge and Opera; roughly 74.5 % of browsers worldwide now have
it. WebUSB is not an alternative — two engines rejected it. **Safari
and iOS are excluded, permanently as far as anyone can see**, because
WebKit opposes both APIs; that is stated plainly in the documentation
rather than papered over, and those users take the CLI path.

**No reusable SMP-over-Web-Serial client exists** (mcumgr-web is
Web-Bluetooth-only, its Web Serial fork was abandoned in 2022,
Golioth's is closed-source, smpjs does not exist), so MCUHome writes
one: roughly a 200-line protocol core — base64 framing with the
`0x0609`/`0x0414` markers, CRC16-ITU-T, 124-byte MTU — plus the image
upload commands. It is spiked in phase 3 and productized in the
dashboard phase. Until then the documented path is the CLI
(mcumgr-class tooling), stated as an interim, not as the product.

**UF2 stays bootstrap-only.** It remains the vendor path that gets a
UF2 board to the standard state, and MCUHome may emit a `.uf2` artifact
as well (`CONFIG_BUILD_OUTPUT_UF2` exists in vanilla Zephyr). But
decision 2 replaces UF2 bootloaders too, so drag-and-drop is not the
steady-state update mechanism for MCUHome devices; serial recovery is.

**ADR 0007 is not weakened by any of this.** The browser client runs in
the user's browser and needs nothing installed; the bootstrap artifacts
are files the builder emits. The one vendor tool a user may touch —
Nordic's `nrfutil` — is a one-time, vendor-side step in generated
instructions, and **never** a build dependency: no script, Makefile
target, workflow or documented contributor step may invoke it (ADR 0013
caveat: modern `nrfutil` is a proprietary Nordic binary). Whether
`adafruit-nrfutil` (BSD-3, active, but speaking **legacy** DFU where the
stock dongle bootloader speaks **secure** DFU) can serve as the open
alternative is an open verification item, to be tested before any
documentation depends on it.

### 6. Commissioning codes are done; the test VIDs are a going-public gate

The commissioning-code half of onboarding is done, not proposed
(builder pairing block, commit `7c9266b`): `mcuhome init-pairing` draws
per-device credentials from the system CSPRNG once, into the user's own
configuration; `mcuhome/model/pairing.py` emits all seven
`CONFIG_CHIP_DEVICE_*` symbols as one indivisible Kconfig group,
because CHIP checks none of them against each other on Zephyr; and
`validate`, `build` and `init-pairing` print the manual pairing code
and the `MT:` QR payload, storing neither.

**VID/PID hygiene before anything ships beyond the bench.** The USB
descriptor still carries Zephyr's test VID `0x2FE3` and the Matter
identity CHIP's test VID `0xFFF1`. Both must be replaced before MCUHome
devices leave a developer's desk — pid.codes is the candidate route for
the USB side; the Matter side is bound to ADR 0012's attestation path,
since a real VID and a real attestation chain are the same conversation.
This is tracked as a **going-public gate**, not a phase-3 blocker: test
VIDs are correct on a bench and wrong in public.

## Consequences

- Onboarding documentation is per board and generated, not one page of
  prose: a board's `BoardDef` knows its bootstrap mechanism, artifact
  format and instructions, so the instructions cannot drift from the
  artifacts the builder actually produces.
- Two states exist per board and both must keep working — coexistence
  (before bootstrap, or permanently on locked bootloaders) and the
  standard state. The partition tables of ADR 0015 differ between them,
  which means the builder must know which state a device is in before
  it can produce an image for it.
- The dongle's bootstrap package is the first artifact MCUHome produces
  that is not a Zephyr image, and its acceptance by the stock Open
  Bootloader is unverified (decision 2). That verification is the gate
  on the whole standard-state plan for the class; the fallback is
  bounded and known.
- MCUHome takes ownership of an SMP client. It is small, but it is a
  protocol implementation the project will maintain for as long as it
  flashes devices — and it is also what makes ADR 0011's provisioning
  responsibility (Thread dataset injection into a not-yet-commissioned
  node) implementable over the same cable, when that is specified with
  the dashboard work.
- Safari/iOS users cannot flash from the browser. That is a product
  limitation with no workaround inside the browser, and it is written
  down where users choose their path, not discovered by them.
- The bootloader and the application diverge in Zephyr version from the
  v4.5 bump onward. ADR 0008's bump task grows one item: re-check
  whether `serial_adapter` has been ported upstream, and unpin if so.
- Related standing decisions: ADR 0007 (host stays at git and docker),
  ADR 0008 (application cadence, bootloader pinned separately),
  ADR 0011 (on-network commissioning needs exactly this transport),
  ADR 0012 (the Matter VID moves with the attestation path),
  ADR 0013 (`nrfutil` may be a user's one-time vendor step, never a
  project dependency), and ADR 0015, which partitions the state this
  ADR brings a board into.
