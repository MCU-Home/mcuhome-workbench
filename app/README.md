# app/

The **generic MCUHome application main** — one file, shared by every
device the builder produces.

| File | Role |
|---|---|
| `src/main.c` | The application glue of every generated device: boot banner, `mcuhome_matter_start()`, `mcuhome_sensor_start()`. Behavior, written once. |
| `CMakeLists.txt` | Refuses, by design — see below. |

## This is not an application

`mcuhome build` generates the device *configuration*
(`src/mcuhome_config.c/.h`: Matter tables and channel bindings) and a
`CMakeLists.txt` that compiles it together with the `src/main.c` above,
reached as `${ZEPHYR_MCUHOME_MODULE_DIR}/app/src/main.c`. So the thing
you build is the generated tree, never this directory:

```sh
mcuhome build <device> --build-dir <dir>     # generates and builds
mcuhome build <device> --build-dir <dir> --generate-only
west build -b <board> -S matter <dir>/app    # the manual equivalent
```

`west build mcuhome/app` was a working command while this directory held
the scaffold's hello-world placeholder. It now fails at CMake configure
time with a message naming the builder — deliberately, because the
alternative is an image that compiles and does nothing.

Building `src/main.c` without a generated `mcuhome_config.h` next to it
is refused a second time, by the compiler, for the same reason.

## What may and may not go in here

May: anything true of *every* MCUHome device, expressible against the
public runtime contracts (`<mcuhome/matter.h>`, `<mcuhome/channel.h>` and
the generated symbols declared in `mcuhome_config.h`).

May not: anything about one device, one board or one peripheral. No
devicetree aliases (not every board has an `led0`), no cluster IDs, no
sensor names. Device-specific behavior is either data in the generated
tables or a component in `components/` — `samples/matter-node/src/main.c`
shows the board-specific variant, where an LED override of the
framework's weak `mcuhome_matter_stage()` hook is legitimate because that
sample is written for exactly one board.
