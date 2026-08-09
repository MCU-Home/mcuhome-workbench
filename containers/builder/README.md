# containers/builder/

The **MCUHome builder image** — the single build environment
([ADR 0007](../../docs/adr/0007-containerized-toolchain.md)). Developers,
CI and the Home Assistant add-on all compile in this image, which is what
makes "works on my machine" and "passes in CI" the same statement.

```sh
docker pull ghcr.io/mcu-home/builder:zephyr-4.4.0-r4   # or build it, below
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
| **a west workspace at `/mcuhome/workspace`** (since r3) | the revisions [`west.yml`](../../west.yml) pins |
| **the contract program at `/mcuhome/run`** (since r4) | [`run`](run), a launcher over `mcuhome.abi` |

Not in it, on purpose: the Zephyr SDK's other ~20 target toolchains and
its host-tool bundle (qemu, openocd — flashing does not happen in a
container, see ADR 0007), and the heavy half of Zephyr's Python
requirements (pyocd, opencv, numpy). The zap **GUI** binary ships in the
same archive as `zap-cli` and is on `PATH`, but does not start: editing a
`.zap` needs a display, so the desktop libraries beyond what headless
`zap-cli` links against are left out (see
`components/matter/zap/README.md`).

## The baked west workspace (r3)

```
/mcuhome/workspace/.west/config
/mcuhome/workspace/mcuhome/                      <- EMPTY: the SDK mounts here
/mcuhome/workspace/zephyr/                       <- patched, tag ref fetched back
/mcuhome/workspace/modules/…                     <- incl. lib/connectedhomeip, patched
/mcuhome/workspace/bootloader/mcuboot/
/mcuhome/workspace.json                          <- what the above actually is
```

Built at image-build time by the same `west init -l` + `west update
--narrow -o=--depth=1` CI runs, with the `matter` group included and both
files in [`patches/`](../../patches/) applied as working-tree changes —
`git apply`, no `--3way`, no fallback, so a drifted patch fails the image
build.

**Why.** Zephyr derives `BUILD_VERSION` from `git describe` in the
workspace and compiles it into the boot banner, so the workspace's git
state is a build input — and one that appears in no build context. The
same example built here and on a CI runner differed for exactly that
reason (measured 2026-08-09: 748960 against 748964 bytes; a shallow clone
cannot see its tag, so `describe` answers with the commit). ADR 0018
promises that a build context plus a pinned container digest reproduces a
build; baking the workspace is what makes that true, because the git state
becomes a property of the image digest.

Three consequences worth knowing:

- **The SDK is not baked.** ADR 0018 makes it a hash-pinned package
  fetched per build, so `/mcuhome/workspace/mcuhome/` is present and
  empty, ready to be mounted into. West re-reads `west.yml` from there on
  *every* CMake configure, so a forgotten mount stops the build at
  configure time naming the missing path — which is the right failure.
- **`.git` stays in every tree** and is not stripped to save space. West
  resolves the `import:` of the `zephyr` project out of git at
  `refs/heads/manifest-rev` rather than off the filesystem, and Zephyr's
  version stamping shells out to `git describe`. The shallow clone keeps
  the tag ref for the two tag-pinned projects, so `describe` answers
  `v4.4.0` and not a commit — that fetch costs no measurable space.
- **`/mcuhome/workspace.json`** records the resolved 40-character commit
  per layer plus the SHA-256 of each applied patch, under the layer names
  the build-container contract defines (`zephyr`, `sdk`, `chip`,
  `mcuboot`). An image digest says "the same", not "what"; two revisions
  in `west.yml` are movable tags, so a rebuild is checkable rather than
  merely trusted. Written by `workspace-record.py`, which is in this
  directory.

**Nothing uses it yet.** `mcuhome build` still bind-mounts the host's west
workspace at its own absolute path and builds out of that, so r3 changes
no build output. Switching the run-time side over — `ZEPHYR_BASE`,
`CCACHE_BASEDIR`, the workspace requirement, the `--workdir`, and
`imgtool`, which `mcuhome sign` resolves out of the *host* workspace
today — is a separate change.

## The contract program at `/mcuhome/run` (r4)

The executable §2.2 of the [build-container
contract](../../docs/design/build-container-contract.md) fixes at that
absolute path, invoked as `/mcuhome/run <action> <absolute path of the
request document>`. It is [`run`](run) in this directory, installed mode
0755 — §2.2 requires it to be executable by *every* user the backend may
exec as — and it is a thin launcher: it puts the mounted SDK on
`PYTHONPATH` and executes `mcuhome.abi`, which is where the invocation
ABI actually lives, because the contract's `subprocess` profile runs the
same code with no image around it.

**One action is implemented: `describe`.** `build` and `verify` answer
`status: "unsupported"`, `reason: "unsupported.action"`, exit 1 — the
legible refusal §7 prescribes for an action a program does not implement.

That is also why the image still carries **no `org.mcuhome.*` labels**.
`org.mcuhome.contract=1` claims conformance, and conformance means all
three actions of §7; a label the program cannot back is a false claim to
exactly the third parties the contract is written for. The labels arrive
with `build` and `verify`.

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
the same commit — and since r3 that rule covers [`west.yml`](../../west.yml)
and [`patches/`](../../patches/) too**, because they are image inputs now.
CI derives "the image needs building" from the tag not existing in the
registry, so a forgotten bump means the tests silently run in the old
environment.

## Building it

```sh
# from the repository ROOT — the context is the repository, not this
# directory, because west.yml and patches/ are image inputs
docker build -t ghcr.io/mcu-home/builder:zephyr-4.4.0-r4 \
    -f containers/builder/Dockerfile .
```

Every tool is downloaded from its upstream and checksum-verified against
the hashes at the top of the `Dockerfile`. Those hashes are the point; an
image that silently absorbs a re-tagged upstream artifact is not
reproducible. `.dockerignore` at the repository root keeps the context to
the ~4 MB that is not `.git` or build output.

What it costs:

| | r2 | r3 |
|---|---|---|
| on disk | 2.7 GB | **5.3 GB** |
| pulled (compressed) | 670 MB | not measured — the first CI push will say |
| workspace stage alone | — | **≈ 3.5 min** |

Both r3 figures were measured on a workstation on 2026-08-09 (`docker
images`; the workspace stage timed from the first, uncached build). The
estimate this section carried before the image existed said ≈ 4.5 GB, so
it was 18 % low: the extrapolation it rested on — one measured shallow
`.git`-to-worktree ratio, applied to every project — does not hold across
thirteen repositories of very different shapes. The number is now
measured, and the reasoning is kept below only because the *choice* it
justifies still stands.

- **On disk.** Full history instead of shallow would be ≈ 6.6 GB, which
  would crowd the runner; 5.3 GB does not (measured 2026-08-09: 34 GB
  free after the workflow's cleanup step) — and
  the baked workspace *replaces* the per-run clone the CI matter job pays
  for today, so the runner's peak drops rather than rises once that job
  stops cloning its own.
- **Pulled size.** r2's 2.7 GB → 670 MB ratio does not transfer:
  worktrees are text and compress well, but a git packfile is already
  deflated and will not compress again inside a layer. Roughly 0.4 GB of
  the image is incompressible packfile. The real number comes from the
  first push rather than from arithmetic.
- **Build time.** The dominant new cost is `west update` with the
  `matter` group — network-bound, serial per project, and the CHIP clone
  brings four submodules. Measured here at ≈ 3.5 minutes on a 1 Gbit/s
  connection; it is a network figure and will differ elsewhere. The tool
  half is unchanged at ≈ 10 min cold.

To use a locally built image, tag it however you like and select it per
build with `--image`, or for a whole shell with
`MCUHOME_BUILDER_IMAGE=…`.

Inspecting what a given image actually carries needs no build:

```sh
docker run --rm ghcr.io/mcu-home/builder:zephyr-4.4.0-r4 \
    cat /mcuhome/workspace.json
```

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
    ghcr.io/mcu-home/builder:zephyr-4.4.0-r4 ccache -s
```

## Bumping Zephyr

The Zephyr pin, the CHIP pin and this image move together (ADR 0008). In
one commit: `west.yml`, `ZEPHYR_LINE` in `mcuhome/container.py`,
`IMAGE_REVISION` back to 1, and the SDK version and checksums in the
`Dockerfile`.
