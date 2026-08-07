# snippets/

Zephyr snippets for connectivity and device-class variants, registered via
`snippet_root` in [zephyr/module.yml](../zephyr/module.yml). Planned snippets
include `wifi`, `thread-ftd`, `thread-mtd` and `thread-sed` (Sleepy End
Device: `CONFIG_OPENTHREAD_MTD`, `CONFIG_OPENTHREAD_MTD_SED`, poll period and
Matter ICD settings).

Device-class variants are configuration, not directory structure: the MCUHome
builder composes snippets, Kconfig fragments and board overlays — it does not
generate per-variant source trees.

Current snippets:

| Snippet | Purpose |
|---|---|
| `matter/` | The numeric/choice Kconfig values the Matter stack needs (mbedTLS heap, stack sizing, p256-m + bignum assembly, picolibc) plus nRF53 802.15.4 workqueue sizing — mandatory for Matter builds (`-S matter`) |
| `debug-rtt/` | RTT log transport (LOG/printk/OpenThread/CHIP over RTT, drop mode, boot-time control-block re-init) — for boards/setups without a free UART (`-S debug-rtt`) |

