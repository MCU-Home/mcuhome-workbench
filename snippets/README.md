# snippets/

Zephyr snippets for connectivity and device-class variants, registered via
`snippet_root` in [zephyr/module.yml](../zephyr/module.yml). Planned snippets
include `wifi`, `thread-ftd`, `thread-mtd` and `thread-sed` (Sleepy End
Device: `CONFIG_OPENTHREAD_MTD`, `CONFIG_OPENTHREAD_MTD_SED`, poll period and
Matter ICD settings).

Device-class variants are configuration, not directory structure: the MCUHome
builder composes snippets, Kconfig fragments and board overlays — it does not
generate per-variant source trees.

Empty until the first snippet is defined.
