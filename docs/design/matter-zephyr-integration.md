# Matter-on-Zephyr Integration — Prototype Findings

> **Status:** Prototype findings backing ADR 0006 and builder-pipeline
> design §4. Compile/link-level result achieved; hardware verification
> (DFU flash + BLE commissioning advertising) in progress.

## Summary

Goal: prove that the upstream Matter SDK
([project-chip/connectedhomeip](https://github.com/project-chip/connectedhomeip),
no Nordic fork) works on vanilla Zephyr v4.4.0 with runtime-dynamic
endpoint registration — i.e. without per-config ZAP codegen — targeting
the nRF52840.

Result: **success at compile/link level**, reached after 17 build
iterations. The upstream SDK builds and links against vanilla Zephyr
(not NCS) with a dynamically registered endpoint, using a small,
enumerable patch set. Hardware bring-up (flashing and BLE commissioning
advertising) had not yet been verified at time of writing.

## Environment

| Component | Version / detail |
|---|---|
| Workspace | west T2 topology |
| Zephyr | v4.4.0, pinned |
| Zephyr SDK | 1.0.1, user-space install, `arm-zephyr-eabi` toolchain only |
| GN | 2502 |
| ZAP | v2025.10.23-nightly, prebuilt (693 MB) |
| CHIP (connectedhomeip) | v1.5.1.0, pinned as a west project with explicit submodules: `nlio`, `nlassert`, `pigweed`, `jsoncpp` (`nlunit-test` no longer exists as a submodule in this tag) |
| Target hardware | nRF52840 (`nrf52840dk`, `nrf52840dongle`) |
| Prototype app | `bridge-app` ZAP file: static endpoint 0 + one dynamically registered temperature endpoint (`emberAfSetDynamicEndpoint`, external attribute storage callbacks, simulated value reported via `MatterReportingAttributeChangeCallback`) |

## Build Result

| Target | Flash | RAM | Notes |
|---|---|---|---|
| `nrf52840dk` | 572,960 B (54.6% of 1 MB) | 151,856 B (57.9% of 256 KB) | Final image after all fixes below |
| `nrf52840dongle` | 588,808 B | — | Built first-try, after all patches were already in place |

Full Matter-over-Thread node fits comfortably in the nRF52840's 1 MB
flash / 256 KB RAM budget.

## Key Findings

### 1. Upstream CHIP has real, first-class Zephyr support
`config/zephyr/chip-module` in upstream CHIP is a proper Zephyr module
(`zephyr/module.yml`, depends on `openthread`); `chip_device_platform =
"zephyr"` is a first-class generic platform, auto-selected for Zephyr
builds. Because the module's `module.yml` is not at the repository root,
it must be registered explicitly via `ZEPHYR_EXTRA_MODULES`.

### 2. Zephyr manifest gaps
- `mbedtls` now requires the companion module `tf-psa-crypto` — added to
  the west manifest allowlist.
- No separate `nrf_802154` module is needed (headers live in
  `hal_nordic`), but `hal_nordic`'s include directories are attached to
  its own library target, not to `zephyr_interface` — CHIP's build-flag
  capture misses them. Fixed by adding those include dirs explicitly in
  a `chip-module` `CMakeLists.txt` patch.

### 3. Missing `python_path.py` helper
CHIP v1.5.1.0's release tarball/tag is missing `python_path.py` (their
CI normally obtains it via the pigweed bootstrap, which this workspace
does not run). Fixed with a small local shim module placed on
`PYTHONPATH`. This is an upstream-worthy bug report.

### 4. ZAP is still required at build time
Even with dynamic-endpoint registration, static endpoint 0 is generated
from a `.zap` file, so ZAP tooling is required at build time. The
prebuilt `zap-cli` download works cleanly with the version pinned in
`scripts/setup/zap.version`. Codegen also needs Python packages `lark`,
`jinja2`, `click`, `coloredlogs`, `stringcase`, plus a `pip install` of
`scripts/py_matter_idl`.

### 5. mbedTLS 4 — biggest source of friction
Zephyr 4.4 ships mbedTLS 4.1, which removes the legacy crypto API in
favor of PSA-only; the legacy implementation survives only as the
`tf-psa-crypto` "builtin driver" under `mbedtls/private/`. CHIP
v1.5.1.0 targets mbedTLS 3.x. Fixes applied:

a. Switched CHIP to its PSA crypto PAL (`chip_crypto = "psa"` in
   `chip-gn/args.gni`) — CHIP ships a complete PSA PAL including PSA
   keystores and PSA Spake2p.
b. Compat shim headers mapping 9 legacy header names (`ecp.h`,
   `bignum.h`, `ccm.h`, `ctr_drbg.h`, `ecdsa.h`, `entropy.h`, `pkcs5.h`,
   `sha1.h`, `sha256.h`) to their `mbedtls/private/` equivalents.
c. Forwarded Zephyr's generated mbedTLS/TF-PSA config defines and
   include paths into the CHIP GN compile via a `chip-module` patch.
   A global `EXTRA_CFLAGS` approach breaks the Zephyr side of the build
   — wrong altitude for the fix.
d. `-DMBEDTLS_DECLARE_PRIVATE_IDENTIFIERS` unlocks the config-gated
   private declarations needed by CHIP's Spake2p ECP math.
e. Two small, version-guarded source patches in
   `CHIPCryptoPALmbedTLSCert.cpp` (`#if MBEDTLS_VERSION_MAJOR >= 4`)
   replacing the removed `mbedtls_pk_ec()` / `mbedtls_pk_get_type()`
   pubkey extraction with `mbedtls_pk_write_pubkey_der()` plus tail
   extraction (P-256).

### 6. GN builds need a pigweed environment stub
GN builds run outside CHIP's own bootstrap need
`build_overrides/pigweed_environment.gni`. An empty stub file suffices
for Zephyr-toolchain builds — the `pw_env_setup_*` variables are only
needed for pigweed-clang host builds.

### 7. Zephyr 4.4 API drift in CHIP's platform layer
`BT_LE_ADV_OPT_CONNECTABLE | BT_LE_ADV_OPT_ONE_TIME` was removed in
favor of `BT_LE_ADV_OPT_CONN`. One-line patch in
`src/platform/Zephyr/BLEManagerImpl.cpp`.

### 8. Kconfig required on vanilla Zephyr
Unlike NCS, vanilla Zephyr sets no defaults for Matter builds. Required:
`CONFIG_BT=y`, `CONFIG_BT_PERIPHERAL=y`, `CONFIG_BT_GATT_DYNAMIC_DB=y`
(for `bt_gatt_service_register`), `CONFIG_NET_L2_OPENTHREAD=y`,
`CONFIG_MBEDTLS=y`, `CONFIG_MBEDTLS_PSA_CRYPTO_C=y`, plus
`PSA_WANT_ALG_HKDF` / `PSA_WANT_ALG_SHA_1` /
`PSA_WANT_ALG_PBKDF2_HMAC` (OpenThread already enables
ECDSA/ECDH/CCM/SHA-256/etc.). `CONFIG_CHIP_FACTORY_DATA` does not exist
upstream (NCS-only) and must be removed from configs.

### 9. Link-order pitfall
`chip_gn.cmake` wraps the Matter libraries in their own
`--start-group`/`--end-group`, placed after `libkernel.a`, leaving
`k_msgq_*` symbols unresolved. Fix: append the kernel library again
*after* the group, referenced as `$<TARGET_FILE:kernel>` — a plain
`kernel` target reference gets deduplicated by CMake against the earlier
occurrence and has no effect.

### 10. ccache not wired into the inner GN build
The Zephyr side of the build picks up ccache automatically; CHIP's inner
GN build bypasses it. The builder must wire a compiler launcher into the
GN args explicitly — recorded as a hard requirement for the MCUHome
builder.

## Implications for MCUHome

- **ADR 0006 answer:** upstream CHIP works without a Nordic fork or NCS
  dependency. Total patch surface: 1 `CMakeLists.txt` patch
  (`chip-module`), 2 guarded source patches, 1 stub file, 9 shim
  headers, 1 Python shim — all small, all automatable by the MCUHome
  builder, and several are upstream-worthy bug reports/fixes.
- Dynamic endpoint registration compiles cleanly against upstream CHIP;
  runtime verification (commissioning, attribute reporting) is pending
  on real hardware.
- Flash budget is confirmed: a full Matter-over-Thread node occupies
  ~573-589 KB, comfortably within the nRF52840's 1 MB flash.
- The builder pipeline must own, as first-class provisioning/config
  concerns: the workspace pins (including `tf-psa-crypto`), the patch
  set (until items are upstreamed), `zap-cli` + GN provisioning, the
  mbedTLS-4 shim layer, and ccache wiring for the GN build.

## Addendum: hardware verification session (2026-08-03/04, nRF52840 dongle)

Runtime verification on the nRF52840 dongle (DFU via Open Bootloader,
12 flashed image revisions) is **paused at a boot-time crash**; compile-level
results above stand. Findings from the hardware phase:

1. **DFU flashing works cleanly** (nrfutil 8.2.0 + nrf5sdk-tools, package
   from `zephyr.hex`); only manual step is the physical RESET button.
2. **Vanilla-Zephyr memory defaults are unusable for Matter**: 1 KB main
   stack, 0 B system heap. Fixed with 8 KB main stack / 2.5 KB workqueue /
   15 KB k_heap. Vendor SDKs set such defaults silently; the MCUHome
   builder must own them.
3. **CHIP's Kconfig silently switches the libc to newlib-nano**
   (`imply NEWLIB_LIBC_NANO`), which dies pre-main on this target.
   Forcing `CONFIG_PICOLIBC=y` (the Zephyr default libc) boots.
4. **The dongle's CDC-ACM console blocks `printk` forever when no host
   terminal is attached** — with console enabled the app hangs at its
   first print. LED-based status signaling was used instead (green solid =
   init, red count = stage number, red solid = stage executing).
5. **Remaining blocker:** with the full Matter config the system freezes
   within ~1 s of boot, *before* the first CHIP API call — an
   asynchronous boot-time subsystem crash. A config without CHIP but with
   BT + OpenThread + mbedTLS survives. Prime suspect: **BLE/802.15.4
   radio coexistence on vanilla Zephyr** (Nordic's dynamic multiprotocol
   arbitration, MPSL, is NCS-proprietary). If confirmed, this materially
   affects ADR 0006: upstream CHIP compiles, but concurrent BLE+Thread
   runtime on nRF52 may require either the Nordic fork/NCS, sequential
   radio use (BLE only during commissioning), or another coexistence
   strategy.
6. **Next step:** move runtime debugging to the nRF5340-DK (on-board
   J-Link: `west flash`, real console, fault backtraces) instead of
   blind LED bisection. The dongle remains a later Thread test node.

## Addendum 2: nRF7002-DK runtime verification — SUCCESS (2026-08-04)

Full Matter-over-Thread node (Thread-only per ADR 0011) **runs on the
nRF7002-DK**: all init stages pass, the dynamically registered
temperature endpoint is live, the event loop runs, simulated values
update with Matter reporting callbacks. Debug pipeline: `west flash`
(J-Link) + RTT console + JLinkExe breakpoint autopsies.

Root causes found and fixed in this phase (each verified by debugger or
source line):

1. **OT radio workqueue stack (512 B default) overflows** on the nRF53
   802.15.4 serialization path (`CONFIG_OPENTHREAD_RADIO_WORKQUEUE_STACK_SIZE`
   → 4096; 2048 still overflowed with immediate logging). No NCS
   precedent — NCS does not exercise this path (own ipc_radio/MPSL).
2. **Spinel backend send-thread stack hardcoded 1024 B** in
   zephyr/modules/hal_nordic (no Kconfig; patched with override guard;
   byte-identical in NCS's fork — upstream issue candidate).
3. **`add_entropy_source` returns 0x6c (UNSUPPORTED_CHIP_FEATURE) in the
   PSA crypto PAL**, and the guards that skip it exist only as NCS
   Kconfig symbols — on vanilla Zephyr + PSA the Zephyr platform's
   InitChipStack ALWAYS fails. Patched to also honor the GN define
   `CHIP_CRYPTO_PSA`. Same wrong-symbol bug silently skipped
   `psa_crypto_init()` in GenericPlatformManagerImpl_ZephyrSelect.ipp —
   also patched. Both upstream-worthy.
4. **CHIP v1.5 requires `initParams.dataModelProvider`**
   (CodegenDataModelProviderInstance) — older example patterns fail with
   0x2f and an explicit hint in the error log.
5. **`CHIP_DEVICE_CONFIG_DYNAMIC_ENDPOINT_COUNT` defaults to 0** —
   dynamic registration fails with 0xb (NO_MEMORY) until a project
   config header raises it (wired via CONFIG_CHIP_PROJECT_CONFIG).
6. nRF5340 app core has **no upstream entropy driver** (TRNG on netcore,
   CryptoCell unsupported upstream, and NCS itself disables CC312 in
   favor of software crypto) — prototype uses a clearly-marked INSECURE
   PRNG driver; production solution is a design work item.
7. RTT/logging notes: deferred logging swallows output of early crashes
   (use LOG_MODE_IMMEDIATE for bring-up); nRF5340 netcore ships
   factory-locked (recover via nrfutil); vanilla Zephyr needs `segger`,
   `open-amp`, `libmetal` manifest modules for RTT + nRF53 IPC.

Commissioning/coexistence strategy from these findings: ADR 0011
(v0.1 on-network via MCUHome-provisioned datasets, BLE off; nrfxlib
multiprotocol later after feasibility analysis).

Remaining for full E2E: Thread dataset provisioning, border router,
HA commissioning test. Dongle (nRF52840) backport expected to benefit
from fixes 3-5 directly.
