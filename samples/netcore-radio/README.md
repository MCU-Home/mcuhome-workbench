# netcore-radio — nRF5340 network-core image

The other half of an MCUHome node on the nRF5340. The application core
runs the Matter firmware; this image runs on the network core and gives
it two things:

| Service | Endpoint on `ipc0` | Provided by |
|---|---|---|
| 802.15.4 radio (spinel serialization) | `nrf_802154_spinel` | `hal_nordic`, enabled by `CONFIG_NRF_802154_SER_RADIO` |
| Entropy | `mcuhome_entropy` | `src/entropy_service.c` |

It **replaces** `zephyr/samples/boards/nordic/ieee802154/802154_rpmsg`.
Everything that sample does, this one does — same `prj.conf` values, same
two fault callouts in `src/main.c` — plus the entropy service. Building
the application core against the upstream image instead produces a node
that boots, joins Thread and then refuses every cryptographic operation.

## Why the entropy service lives here

The nRF5340's RNG peripheral is wired to the network core; the
application core has none (`dts/arm/nordic/nrf5340_cpuapp.dtsi` points
`zephyr,entropy` at the Bluetooth HCI entropy driver, which is no help in
a Thread-only build with Bluetooth off — ADR 0011). Upstream Zephyr has
no equivalent of `entropy_bt_hci.c` for the 802.15.4 IPC channel, so
MCUHome provides one: this service on the radio side,
`mcuhome,entropy-ipc` (`drivers/entropy/`) on the application side, wire
protocol in `include/mcuhome/entropy_ipc.h`.

Design points worth knowing before changing anything here:

- **Second endpoint, not a second instance.** It rides on `ipc0`, the
  instance the spinel channel already uses. No extra shared memory, no
  extra mailbox channel. `CONFIG_IPC_SERVICE_BACKEND_RPMSG_NUM_ENDPOINTS_PER_INSTANCE`
  defaults to 2 and Bluetooth is off, so the second slot is free and no
  Kconfig change is needed.
- **Own thread.** Requests are handed from the IPC receive callback to a
  dedicated thread. The callback runs on the mailbox workqueue, which the
  spinel receive path shares — blocking it on the RNG (tens of
  microseconds per byte) would stall the radio. The same reason Nordic's
  spinel backend keeps its own send thread.
- **Init order.** `SYS_INIT` at `POST_KERNEL` priority 60, after
  hal_nordic's serialization init at `CONFIG_NRF_802154_SER_RADIO_INIT_PRIO`
  (53). That one opens the IPC instance; on this core the backend has the
  `remote` role and opening spins until the application core is up, so it
  must happen in exactly one place. A `BUILD_ASSERT` pins the ordering.
- **Silent by design.** `CONFIG_LOG=n` and no console, inherited from the
  upstream sample. A protocol error is answered with `nbytes = 0` and
  reported loudly on the application core, which has the RTT log.

## Build

Both images are separate builds. Nothing links them at build time — they
meet at the IPC instance — so the order does not matter here.

```sh
# from the west workspace top directory
west build -p -b nrf7002dk/nrf5340/cpunet mcuhome/samples/netcore-radio \
    -d build/netcore

west build -p -b nrf7002dk/nrf5340/cpuapp -S matter -S debug-rtt \
    mcuhome/samples/matter-node -d build/app
```

The network-core build needs neither `PYTHONPATH=.../pyshim` nor `zap` on
`PATH` — there is no CHIP here. Only the Zephyr SDK.

`nrf5340dk/nrf5340/cpunet` builds too (same SoC, same IPC layout); the
DK's application core needs its own board overlay for the entropy node,
which this repository does not ship yet.

## Flash

Two images, two cores, one debug probe. Both go through the on-board
J-Link, and `west flash` picks the right core from the build directory:
the flashed hex carries the core's own address range, and the board's
`board.cmake` additionally passes `--device=nrf5340_xxaa_net` for the
`cpunet` build (`nrf5340_xxaa_app` for `cpuapp`) to the `jlink` runner.

```sh
west flash -d build/netcore     # network core
west flash -d build/app         # application core
```

**Order: network core first, application core second.** Not a hard
requirement — the application core's IPC endpoints bind whenever the peer
appears — but it means the first boot after flashing is a boot with both
services present, instead of one where the entropy driver spends its
`CONFIG_MCUHOME_ENTROPY_SEED_TIMEOUT_S` on a core that is about to be
reprogrammed anyway. Flashing the application core resets it, so no
manual reset is needed afterwards.

`west flash` defaults to the `nrfutil` runner on this board
(`build/*/zephyr/runners.yaml`); `--runner jlink` and `--runner nrfjprog`
are the alternatives.

### If the network core is locked

nRF5340 devices ship with the network core under readback protection,
and a debugger that cannot see through it reports it as inaccessible —
`probe-rs` says **"Core 1 is locked"** on the full-chip target and
refuses. That is a device state, not a probe problem, and it survives
flashing the application core. Clear it once per device:

```sh
nrfutil device recover --core network
nrfutil device recover --core application   # only if that one is locked too
```

`recover` erases the core it is given, so re-flash both images afterwards.
Check the state first if you would rather not erase:

```sh
nrfutil device protection-get --core network
```

`nrfutil` (8.2.0 here) needs its `device` command installed:
`nrfutil install device`. `nrfjprog --recover --coprocessor CP_NETWORK`
is the equivalent for the older toolchain.

## Verifying the pair on hardware

The application core logs over RTT (`-S debug-rtt`). A working pair shows,
from `drivers/entropy/`:

```
<inf> mcuhome_entropy: CTR-DRBG seeded from netcore entropy service
```

A network core running the wrong image gives the opposite, once per
entropy request after the timeout expires:

```
<err> mcuhome_entropy: no seed from netcore entropy service within 10 s — refusing to hand out randomness
```

and a version skew between the two builds gives:

```
<err> mcuhome_entropy: entropy protocol mismatch: peer magic 0x...., expected 0x....
```
