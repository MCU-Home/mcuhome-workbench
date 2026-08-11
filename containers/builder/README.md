# containers/builder/

The **MCUHome builder image** — the single build environment
([ADR 0007](../../docs/adr/0007-containerized-toolchain.md)). Developers,
CI and the Home Assistant add-on all compile in this image, which is what
makes "works on my machine" and "passes in CI" the same statement.

```sh
docker pull ghcr.io/mcu-home/builder:zephyr-4.4.0-r7   # or build it, below
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
| **the contract program at `/mcuhome/run`** (since r4) | [`run`](run), a launcher over `mcuhome.compiler.abi` |

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
`PYTHONPATH` and executes `mcuhome.compiler.abi`, which is where the
invocation ABI actually lives, because the contract's `subprocess`
profile runs the same code with no image around it.

That module path is why **r6** exists: ADR 0020's package split moved the
ABI from `mcuhome.abi` into `mcuhome.compiler.abi`, and this launcher is
the one piece of *image* content that spells it. The module it launches
is not image content — it arrives with the SDK mount — so nothing else
about r6 differs from r5.

**All three actions of §7 are implemented** — `describe`, `verify` and
`build` — which is what let the image start claiming conformance in r5.
The claim was withheld through r4 on purpose: `org.mcuhome.contract=1`
claims conformance, conformance means all three actions, and a label the
program cannot back is a false claim to exactly the third parties the
contract is written for.

## The static self-description at `/mcuhome/describe.json` (r7)

§2.2.1 of the contract lets an image carry its `describe` answer as a
file, and this image does. It holds "exactly what a `describe`
invocation answers", and the Dockerfile keeps that true the only way that
needs no discipline: it **runs** `describe` at image build time, with the
program body borrowed from the build context and the baked
`/mcuhome/workspace.json` as its record, and stores the result document
unread at mode 0644. There is no second implementation of the `program`
block to drift away from the first.

It exists because this image is §6.1's own split: the launcher is image
content, the program *body* arrives with the SDK mount. A backend that
has not yet chosen a mount point therefore cannot ask this image
anything — while `trees` in the answer is precisely what tells it where
the mount has to go. `describe` stays authoritative (§7.1); the file is
pre-start data, like the labels.

The three coupling labels were repaired in the same revision, because
they are the other half of what a backend may learn before it starts a
container:

| Label | r5 and r6 | since r7 |
|---|---|---|
| name | `org.mcuhome.model.toolchain` | `org.mcuhome.toolchain` (§2.1) |
| `org.mcuhome.zephyr` | `v4.4.0` | `4.4.0` — §2.1.1 asks for the version *without* west's leading `v` |
| `org.mcuhome.toolchain` | `zephyr-sdk-1.0.1/arm-zephyr-eabi` | `zephyr-sdk-1.0.1` — `<identity>-<version>`, and `/` is outside the permitted character class |

None of the three was cosmetic. A compatibility constraint is evaluated
against these values, and "a container that does not carry a named label
does not qualify" — so under the old spellings this image satisfied no
SDK release's constraint at all. The target triple is not lost with the
old value: it is the `ZEPHYR_TOOLCHAIN` build argument at the top of the
`Dockerfile`, which is where an image input belongs.

## Versioning

`ghcr.io/mcu-home/builder:zephyr-<zephyr version>-r<image revision>`

The Zephyr part is the `zephyr` revision pinned in
[`west.yml`](../../west.yml); the revision counts rebuilds of the image
for that same Zephyr release. The single source of truth for both is
[`mcuhome/compiler/container.py`](../../mcuhome/compiler/container.py) — the builder, the CI
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

Since r7 the rule reaches one step further, and only one: the baked
`describe.json` is the program's self-description, so it goes stale when
the `program` block does — the shared release version, or one of the ABI
constants behind `contract`, `request`, `result` and `actions`.
Everything else in `mcuhome/` can change without touching this image,
because the body arrives with the SDK mount. The version half needs no
discipline of its own: one tag cuts the wheels, the SDK archive and this
image (ADR 0020 decision 8).

## Building it

```sh
# from the repository ROOT — the context is the repository, not this
# directory, because west.yml and patches/ are image inputs
docker build -t ghcr.io/mcu-home/builder:zephyr-4.4.0-r7 \
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
docker run --rm ghcr.io/mcu-home/builder:zephyr-4.4.0-r7 \
    cat /mcuhome/workspace.json
```

## How the builder runs it

`mcuhome build` assembles the invocation in
[`mcuhome/compiler/container.py`](../../mcuhome/compiler/container.py); `mcuhome build …`
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
    ghcr.io/mcu-home/builder:zephyr-4.4.0-r7 ccache -s
```

## Bumping Zephyr

The Zephyr pin, the CHIP pin and this image move together (ADR 0008). In
one commit: `west.yml`, `ZEPHYR_RELEASE` in `mcuhome/compiler/container.py`,
`IMAGE_REVISION` back to 1, and the SDK version and checksums in the
`Dockerfile`.
