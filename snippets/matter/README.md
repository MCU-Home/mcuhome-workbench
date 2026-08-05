# `matter` snippet

Numeric and choice Kconfig defaults for a Matter (CHIP) node that vanilla
Zephyr ships too small, off, or unset for CHIP to run reliably — mbedTLS
heap size and crypto acceleration, stack/heap sizing, and (on nRF5340) the
OpenThread radio workqueue stack; these are plain integer/bool values a
user may still want to override, so they ship as a snippet rather than
being forced from `components/matter/Kconfig` (a Kconfig `select` cannot
carry a numeric value). Apply with `west build -S matter ...`; see
[docs/design/matter-zephyr-integration.md](../../docs/design/matter-zephyr-integration.md)
for the findings behind each setting.
