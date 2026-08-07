# containers/builder/

The **MCUHome builder image** — the single build environment
([ADR 0007](../../docs/adr/0007-containerized-toolchain.md)). Developers,
CI and the Home Assistant add-on all compile in this image, which is what
makes "works on my machine" and "passes in CI" the same statement.

```sh
docker pull ghcr.io/mcu-home/builder:zephyr-4.4.0-r1   # or build it, below
mcuhome build <device>                                 # uses it by default
```

## What is in it, and what is not

| Provides | Version |
|---|---|
| Zephyr SDK, `arm-zephyr-eabi` toolchain only | 1.0.1 |
| CMake, Ninja, dtc, gperf, host GCC (for `native_sim`) | Debian 13 stock |
| west and Zephyr's Python requirements | pinned in `requirements.txt` |
| `gn` — the Matter SDK builds its libraries with it | `2502 (17b0057970fa)` |
| `zap-cli` — Matter root-node data model | `v2025.10.23-nightly` |
| ccache | Debian 13 stock |

It contains **tools only**. No MCUHome sources, no west workspace, no
Zephyr checkout: the workspace is bind-mounted at run time, at the same
absolute path it has on the host, so a build directory produced inside the
container is a perfectly ordinary build directory outside it.

Not in it, on purpose: the Zephyr SDK's other ~20 target toolchains and
its host-tool bundle (qemu, openocd — flashing does not happen in a
container, see ADR 0007), and the heavy half of Zephyr's Python
requirements (pyocd, opencv, numpy). The zap **GUI** binary ships in the
same archive as `zap-cli` and is on `PATH`, but does not start: editing a
`.zap` needs a display, so the desktop libraries beyond what headless
`zap-cli` links against are left out (see
`components/matter/zap/README.md`).

## Versioning

`ghcr.io/mcu-home/builder:zephyr-<zephyr version>-r<image revision>`

The Zephyr part is the `zephyr` revision pinned in
[`west.yml`](../../west.yml); the revision counts rebuilds of the image
for that same Zephyr release. The single source of truth for both is
[`mcuhome/container.py`](../../mcuhome/container.py) — the builder, the CI
workflow and this file all read the tag from there, and `tests_py/
test_container.py` asserts that it still agrees with `west.yml`.

There is no `latest`: a build environment that changes under a stable
name is not a build environment. CI additionally publishes the moving
`:zephyr-<zephyr version>` alias, which the builder never asks for.

**Changing anything in this directory means bumping `IMAGE_REVISION` in
the same commit.** CI derives "the image needs building" from the tag not
existing in the registry, so a forgotten bump means the tests silently run
in the old environment.

## Building it

```sh
docker build -t ghcr.io/mcu-home/builder:zephyr-4.4.0-r1 containers/builder
```

Roughly 10 minutes on a cold cache — most of it downloading — and about
2.7 GB on disk (670 MB pulled). The build context is this directory: every
tool is downloaded from its upstream and checksum-verified against the
hashes at the top of the `Dockerfile`. Those hashes are the point; an
image that silently absorbs a re-tagged upstream artifact is not
reproducible.

To use a locally built image, tag it however you like and select it per
build with `--image`, or for a whole shell with
`MCUHOME_BUILDER_IMAGE=…`.

## How the builder runs it

`mcuhome build` assembles the invocation in
[`mcuhome/container.py`](../../mcuhome/container.py); `mcuhome build …`
prints it before it runs. In short:

- `--user <your uid>:<your gid>` — nothing is left behind owned by root.
- the workspace mounted onto itself, and the build directory too when it
  lives somewhere else.
- the ccache bind-mounted from `~/.cache/mcuhome/ccache`
  (`MCUHOME_CCACHE_DIR` moves it), so it survives the container. A
  directory rather than a named volume, because a fresh named volume is
  root-owned and a container running as you cannot write to it.
- an environment that is composed, not inherited: only `ZEPHYR_BASE`,
  `PYTHONPATH`, `HOME` and the ccache settings, all of which depend on
  where the workspace is. Everything else belongs to the image.

## ccache

ccache is a hard requirement of the builder, not an optimization
(builder-pipeline.md §5): on a Raspberry-class Home Assistant host it is
the difference between usable and painful. Both halves of the build go
through it:

- **Zephyr/CMake** finds ccache by itself (`zephyr/cmake/modules/
  ccache.cmake` sets the global `RULE_LAUNCH_COMPILE` property).
- **CHIP's inner GN build** is a separate build system and sees none of
  that. The generated application's `CMakeLists.txt` pre-seeds
  `MATTER_GN_ARGS` with Pigweed's `pw_command_launcher` GN argument
  before `find_package(Zephyr)` pulls the CHIP module in — see the
  comment there. Without it, every clean build directory recompiles the
  whole Matter stack.

`-DUSE_CCACHE=0` (Zephyr's own switch) turns off both.

The image's `/etc/ccache.conf` carries one non-obvious line,
`ignore_options = -specs=*`, without which **a Zephyr build caches
nothing at all**: `-specs=picolibc.specs` is a bare file name that the
toolchain resolves and ccache does not, so ccache fails to stat it and
refuses every compile with "bad compiler arguments". The `Dockerfile`
says the same thing at more length, including why excluding it from the
hash is safe.

```sh
# what the cache is doing, with the same mount the builder uses
docker run --rm --user "$(id -u):$(id -g)" \
    --volume ~/.cache/mcuhome/ccache:/ccache \
    ghcr.io/mcu-home/builder:zephyr-4.4.0-r1 ccache -s
```

## Bumping Zephyr

The Zephyr pin, the CHIP pin and this image move together (ADR 0008). In
one commit: `west.yml`, `ZEPHYR_LINE` in `mcuhome/container.py`,
`IMAGE_REVISION` back to 1, and the SDK version and checksums in the
`Dockerfile`.
