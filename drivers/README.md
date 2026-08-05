# drivers/

Out-of-tree Zephyr device drivers (each with its own Kconfig, CMakeLists.txt
and devicetree binding under [dts/bindings/](../dts/bindings/)).

| Driver | Notes |
|---|---|
| `entropy/` | `mcuhome,proto-entropy` — INSECURE placeholder PRNG for SoCs without an upstream entropy driver (nRF5340 app core). Gated behind `CONFIG_MCUHOME_ALLOW_INSECURE_ENTROPY`, development only. |
