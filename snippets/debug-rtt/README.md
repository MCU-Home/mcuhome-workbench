# `debug-rtt` snippet

Routes Zephyr logging (LOG, printk) plus OpenThread and CHIP module log
output through SEGGER RTT instead of a UART console, with the RTT-buffer
sizing and drop-mode settings needed to survive Matter/OpenThread log
load without wedging, and enables `MCUHOME_RTT_REINIT` (lib/debug/) so a
reset doesn't leave the RTT control block stuck with stale offsets. Apply
with `west build -S debug-rtt ...`.
