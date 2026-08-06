# drivers/

Out-of-tree Zephyr device drivers (each with its own Kconfig, CMakeLists.txt
and devicetree binding under [dts/bindings/](../dts/bindings/)).

| Driver | Notes |
|---|---|
| `entropy/` | `mcuhome,entropy-ipc` — entropy for cores without an RNG peripheral of their own (nRF5340 app core). Seeds a CTR-DRBG from a peer core over an `ipc_service` endpoint; the peer runs [`samples/netcore-radio/`](../samples/netcore-radio/). Fails loud (`-EIO`) if no seed arrives — there is no non-cryptographic fallback. |

`entropy/` is split in two on purpose: `entropy_ipc_core.c` holds the DRBG
and the seeded/unseeded state machine and knows nothing about IPC, so it
builds and runs on the host ([`tests/entropy_ipc/`](../tests/entropy_ipc/));
`entropy_mcuhome_ipc.c` is the transport plus the Zephyr driver plumbing.
The seam between them (`struct mcuhome_entropy_seed_source`) is also where
a different seed provider would be substituted — see ADR 0013 on blob
policy for the `nrf_cc3xx` case.
