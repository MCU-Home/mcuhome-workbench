# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# The MCUHome builder image (ADR 0007): the single build environment used
# by developers, CI and the Home Assistant add-on. Since r3 it carries two
# things: the TOOLS (Zephyr SDK, west, gn, zap, ccache) and a real WEST
# WORKSPACE at /mcuhome/workspace — Zephyr, the modules it imports, MCUboot
# and the Matter SDK, at the revisions west.yml pins, with the patch set in
# patches/ applied. What it does NOT carry is MCUHome itself: the manifest
# repository's directory (/mcuhome/workspace/mcuhome) is present and empty,
# because ADR 0018 makes the SDK a hash-pinned package fetched per build
# and mounted there at run time.
#
# Why the workspace is baked (ADR 0020 decision E5). Zephyr stamps
# BUILD_VERSION from `git describe` in the workspace and compiles it into
# the boot banner, so the git state of the workspace is a build input —
# one that appears in no build context. The same example built from a full
# checkout and from CI's shallow one differ (measured 2026-08-09: 748960
# against 748964 bytes), which contradicts ADR 0018's promise that a
# context plus a pinned container digest reproduces the build. Baked, that
# state becomes a property of the image digest, and the gap closes.
#
# Nothing uses the baked workspace yet: mcuhome/compiler/container.py still
# bind-mounts the host's workspace at its own absolute path and builds out
# of that (which is what makes a build directory usable from inside and
# outside the container). Switching the run-time side over is a separate
# change; until it lands, this workspace is carried and not read.
#
# Since r4 there is a third thing: the PROGRAM of the build-container
# contract at /mcuhome/run (docs/design/build-container-contract.md §2.2),
# a thin launcher over mcuhome.compiler.abi, which is where the invocation ABI and
# all three actions of §7 live. The abi module arrives with the SDK mount,
# not with this image, so what the image contributes here is the launcher
# and the fixed path. Since r5 the image also carries the org.mcuhome.*
# labels of §2.1 — see the LABEL block in stage 3 for what that claim
# means and what it deliberately does not.
#
# Since r7 there is a fourth: /mcuhome/describe.json, §2.2's optional
# static self-description, generated at image build time by running the
# program's own `describe` (end of stage 2). It is what lets a backend
# read this image's `program` block before it has a mount point to put
# the SDK at — the labels of the same revision were repaired in the same
# breath, because both are the pre-start half of §2.1/§2.2.
#
# Versioned in lockstep with the Zephyr pin in west.yml: the tag is
# `zephyr-<zephyr version>-r<image revision>`, and the single source of
# truth for it is mcuhome/compiler/container.py. Bump the revision whenever
# anything below changes; bump the Zephyr part when west.yml does. Since
# r3 the revision also has to be bumped for west.yml and patches/, which
# are image inputs now.
#
# Since r7 that rule reaches one step further, and only one: the baked
# describe.json is the program's self-description, so it goes stale when
# the `program` block does — the shared release version, or one of the
# ABI constants behind `contract`, `request`, `result` and `actions`.
# Everything else in mcuhome/ can change without touching this image,
# because the body arrives with the SDK mount. The version half needs no
# discipline: one tag cuts the wheels, the SDK archive and this image
# (ADR 0020 decision 8), so a release rebuilds it anyway.
#
# Build (from the repository root — the context is the REPOSITORY, not
# this directory, because west.yml and patches/ live outside it):
#     docker build -t ghcr.io/mcu-home/builder:zephyr-4.4.0-r7 \
#         -f containers/builder/Dockerfile .
#
# Everything downloaded is checksum-verified. The checksums are the whole
# point of the fetch stage: an image that silently absorbs a re-tagged
# upstream artifact is not a reproducible build environment.

# --------------------------------------------------------------------------
# Versions — every one of them pinned on purpose
# --------------------------------------------------------------------------
#
# Debian 13 (trixie) rather than Ubuntu or Alpine: it is the oldest
# still-current distribution whose stock packages already satisfy Zephyr
# v4.4.0's minimums (CMake >= 3.20.5, Python >= 3.12, dtc >= 1.4.6) — so
# no third-party apt repository, no hand-built CMake, and glibc rather
# than musl, which the prebuilt Zephyr SDK and the prebuilt Electron
# binary that is zap both require. Pinned by digest, not by tag: `trixie-
# slim` moves with every point release.
#
# Not used as a base: ghcr.io/zephyrproject-rtos/ci. It carries every
# toolchain Zephyr supports plus vendor tooling (>10 GB) and its own
# release cadence; MCUHome needs one architecture and owns its pins.
ARG DEBIAN_IMAGE=debian:trixie-slim@sha256:3a39a0592364683e6bab97937b72cad5a8fa6dcbbee90edb3bb48c7f8e94f258

# Zephyr SDK: the *minimal* bundle plus exactly one target toolchain.
# The full bundle is ~20 architectures and several gigabytes; MCUHome
# targets ARM today (nRF53/nRF54) and grows this list one entry at a time
# when a new architecture ships. The SDK's own host tools (qemu, openocd,
# a second dtc) are deliberately not installed — Debian's dtc and gperf
# are what the build uses, and flashing does not happen in this container
# (ADR 0007).
ARG ZEPHYR_SDK_VERSION=1.0.1
ARG ZEPHYR_SDK_SHA256=ca9bc0ff66fafca1dac9d592a36d953cf16d096a9d09b1c0357f021cf9f6a7eb
ARG ZEPHYR_TOOLCHAIN=arm-zephyr-eabi
ARG ZEPHYR_TOOLCHAIN_SHA256=21b85981cb5a1818d9bc53d82af80f208946ec038b982ff1907287572ed3a634

# gn: the Matter SDK builds its own libraries with it (ADR 0006). Taken
# from the CIPD package Chromium infrastructure publishes, pinned by the
# gn git revision — `gn --version` reports `2502 (17b0057970fa)`, which
# is the version the MCUHome prototype was verified with. Pigweed's own
# pin inside CHIP v1.5.1.0 is older (git_revision:97b68a0b); MCUHome
# tracks what it tested rather than what a vendored submodule suggests.
ARG GN_REVISION=17b0057970fa2b07a20cbb4289ab78cf93565f35
ARG GN_SHA256=66cba1384b08f05643a31627bdec1d0fcc87ae7d40853ec24ec60b6eb02fe6c9

# zap: generates the Matter root-node data model from the framework's
# .zap (components/matter/zap/). The version is CHIP v1.5.1.0's own pin,
# `scripts/setup/zap.version` — reading it from anywhere else would let
# the generated data model drift away from the SDK that consumes it.
ARG ZAP_VERSION=v2025.10.23-nightly
ARG ZAP_SHA256=fe327379a07f22fb91661b6a80c1e58c8f84f5af1d8dd1288b7fb69c8ef75ac0

# The two projects west.yml pins by TAG rather than by SHA. West resolves
# them from the manifest — nothing here would need to know them, except
# that a shallow fetch leaves the tag behind at the remote and the
# workspace stage has to fetch it back (see there for why that matters).
# They double as the version part of the patch file names, which is the
# naming convention patches/README.md documents.
#
# Restating a pin is a drift risk, so it is checked: tests_py/
# test_builder_workspace.py asserts both against west.yml, the same way
# tests_py/test_container.py asserts the image tag against it.
ARG ZEPHYR_REVISION=v4.4.0
ARG CHIP_REVISION=v1.5.1.0

# --------------------------------------------------------------------------
# Stage 1: fetch and unpack, so the final image never carries the archives
# --------------------------------------------------------------------------
FROM ${DEBIAN_IMAGE} AS fetch

ARG ZEPHYR_SDK_VERSION
ARG ZEPHYR_SDK_SHA256
ARG ZEPHYR_TOOLCHAIN
ARG ZEPHYR_TOOLCHAIN_SHA256
ARG GN_REVISION
ARG GN_SHA256
ARG ZAP_VERSION
ARG ZAP_SHA256

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        ca-certificates \
        curl \
        unzip \
        xz-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /fetch

# Zephyr SDK. The toolchain archive unpacks to `<toolchain>/`, and the SDK
# expects it under `gnu/` — that is exactly what the SDK's own setup.sh
# does with it, minus the interactive prompts and the host-tool installer.
RUN sdk_base="https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v${ZEPHYR_SDK_VERSION}" \
 && curl -fsSL -o sdk.tar.xz \
        "${sdk_base}/zephyr-sdk-${ZEPHYR_SDK_VERSION}_linux-x86_64_minimal.tar.xz" \
 && echo "${ZEPHYR_SDK_SHA256}  sdk.tar.xz" | sha256sum -c - \
 && mkdir -p /opt \
 && tar -xJf sdk.tar.xz -C /opt \
 && curl -fsSL -o toolchain.tar.xz \
        "${sdk_base}/toolchain_gnu_linux-x86_64_${ZEPHYR_TOOLCHAIN}.tar.xz" \
 && echo "${ZEPHYR_TOOLCHAIN_SHA256}  toolchain.tar.xz" | sha256sum -c - \
 && mkdir -p "/opt/zephyr-sdk-${ZEPHYR_SDK_VERSION}/gnu" \
 && tar -xJf toolchain.tar.xz -C "/opt/zephyr-sdk-${ZEPHYR_SDK_VERSION}/gnu" \
 && rm -rf /opt/zephyr-sdk-${ZEPHYR_SDK_VERSION}/hosttools sdk.tar.xz toolchain.tar.xz

# gn — one static binary.
RUN curl -fsSL -o gn.zip \
        "https://chrome-infra-packages.appspot.com/dl/gn/gn/linux-amd64/+/git_revision:${GN_REVISION}" \
 && echo "${GN_SHA256}  gn.zip" | sha256sum -c - \
 && mkdir -p /opt/gn \
 && unzip -q gn.zip -d /opt/gn \
 && chmod +x /opt/gn/gn \
 && rm gn.zip

# zap — a packaged Electron application; the zip is flat, so it unpacks
# straight into its own directory. The .deb of the same release is not
# used: it ships only the GUI binary, not the zap-cli the build calls.
RUN curl -fsSL -o zap.zip \
        "https://github.com/project-chip/zap/releases/download/${ZAP_VERSION}/zap-linux-x64.zip" \
 && echo "${ZAP_SHA256}  zap.zip" | sha256sum -c - \
 && mkdir -p /opt/zap \
 && unzip -q zap.zip -d /opt/zap \
 && chmod +x /opt/zap/zap /opt/zap/zap-cli \
 && rm zap.zip

# --------------------------------------------------------------------------
# Stage 2: the west workspace, materialised exactly the way CI does it
# --------------------------------------------------------------------------
#
# Kept apart from the final stage for the same reason as the fetch stage:
# what it needs to do the job (a second Python environment, pip's network
# access, the manifest copy that gets thrown away again) must not end up
# in the image. Only /mcuhome survives, by COPY --from.
FROM ${DEBIAN_IMAGE} AS workspace

ARG ZEPHYR_REVISION
ARG CHIP_REVISION

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Only what `west update` itself needs. No compiler: nothing is built
# here, the trees are fetched and patched and that is all.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        ca-certificates \
        git \
        python3 \
        python3-venv \
 && rm -rf /var/lib/apt/lists/*

# west, at the version the image ships, without restating the pin: the
# image's requirements file is used as a *constraint* file, so `pip
# install west` can only resolve to the line already in it. One west lays
# the workspace out and the same west reads it later — a version skew
# between the two would be a difference nobody could see from outside.
#
# ruamel.yaml comes along for the LAST step of this stage, which runs the
# program's own `describe` (see there): `mcuhome.compiler.abi` imports
# `mcuhome.workbench.api` at module level, and the workbench's one
# third-party dependency is that parser. Same constraint file, so the
# parser this stage imports and the one the image ships are one version.
COPY containers/builder/requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/west-venv \
 && /opt/west-venv/bin/pip install --no-cache-dir --disable-pip-version-check \
        --constraint /tmp/requirements.txt west ruamel.yaml
ENV PATH=/opt/west-venv/bin:/usr/local/bin:/usr/bin:/bin

# `west init -l` needs exactly one file of the manifest repository:
# west.yml. It checks that the file is there and writes .west/config; it
# never asks whether the directory is a git repo (west/app/project.py,
# west 1.5.0). That is what makes it possible to lay the workspace out
# here from a bare copy of the manifest and mount the real SDK in later.
WORKDIR /mcuhome/workspace
COPY west.yml /mcuhome/workspace/mcuhome/west.yml
RUN west init -l mcuhome

# The same invocation the CI matter job runs, and deliberately WITHOUT a
# group-filter: the `matter` group is wanted here — this is the image that
# builds Matter firmware.
#
# --narrow -o=--depth=1 because full history for the thirteen projects is
# ~3.9 GB, 2.5 GB of it .git that no build ever reads. What a build DOES
# read out of .git stays: west resolves the `import:` of the zephyr
# project out of git at refs/heads/manifest-rev, never off the filesystem
# (west/manifest.py), and Zephyr's version stamping shells out to `git
# describe`. Stripping .git to save space would break both.
#
# Note for a rebuild: docker caches this layer on the COPY above, so an
# unchanged west.yml reuses whatever the tags resolved to last time. That
# is why /mcuhome/workspace.json records the resolved commits — with two
# movable tags in the manifest, a rebuild is checkable rather than
# trustworthy.
RUN west update --narrow -o=--depth=1

# A shallow fetch brings no tags along, and `git describe --abbrev=12
# --always` (zephyr/cmake/modules/git.cmake) then answers with the commit
# instead of the tag. That string becomes BUILD_VERSION, gets compiled
# into the boot banner (zephyr/kernel/banner.c) and lands in the firmware:
# measured 2026-08-09, the same example built shallow is 4 bytes larger
# than built from a full checkout. Fetching just the tag ref costs
# nothing measurable — .git stayed at 125 MB in the same measurement —
# and makes the two byte-identical.
#
# Only these two: every other project is pinned by SHA, where `git
# describe --always` already answers with that SHA either way.
#
# The two tags are not the same kind of object, and only one of them
# changes what a build produces. Zephyr's v4.4.0 is annotated, so plain
# `git describe` finds it and BUILD_VERSION reads `v4.4.0` — that is the
# byte-identity above. CHIP's v1.5.1.0 is lightweight, so `git describe`
# passes over it and answers with the commit unless `--tags` is given
# (both verified inside the built image, 2026-08-09). Its ref is fetched
# anyway: it costs nothing, and a tree that knows the tag it is pinned to
# is worth more than one that only knows a hash.
#
# The remote is asked for rather than named. west calls it after the
# manifest's `remotes:` entry — `zephyrproject-rtos` for zephyr,
# `project-chip` for connectedhomeip (west.yml, verified against a
# materialised workspace 2026-08-09) — and never `origin`. Hardcoding
# either name would make this step depend on a manifest detail that has
# nothing to do with it; each project has exactly one remote, so taking
# the first is both correct and indifferent to what it is called.
RUN set -eu; \
    for entry in "zephyr:${ZEPHYR_REVISION}" \
                 "modules/lib/connectedhomeip:${CHIP_REVISION}"; do \
        project="${entry%%:*}"; \
        tag="${entry##*:}"; \
        remote="$(git -C "$project" remote | head -n1)"; \
        test -n "$remote"; \
        git -C "$project" fetch --depth=1 "$remote" \
            "refs/tags/$tag:refs/tags/$tag"; \
    done

# The patch set, applied the way CI applies it (.github/workflows/ci.yml)
# and patches/README.md documents it: `git apply`, nothing else — no
# --3way, no --reject, no retry. A patch that has drifted from the
# revision west.yml pins has to fail the IMAGE build, loudly, right here.
#
# Left as working-tree changes, NOT committed. Committing moves `git
# describe` from `v4.4.0` to `v4.4.0-1-g<sha>` and therefore changes
# BUILD_VERSION, the banner and the firmware (measured 2026-08-09); it
# would also need pinned committer identity and dates to produce a
# reproducible SHA. Uncommitted reproduces exactly the tree the bench node
# was commissioned from, and `git status` inside a layer still shows the
# patch set the way patches/README.md says it does.
COPY patches /tmp/patches
RUN git -C zephyr apply --verbose \
        "/tmp/patches/zephyr-${ZEPHYR_REVISION}-nrf53-spinel-stack.patch" \
 && git -C modules/lib/connectedhomeip apply --verbose \
        "/tmp/patches/connectedhomeip-${CHIP_REVISION}-vanilla-zephyr.patch"

# What the image says about the workspace it carries: the resolved commit
# per layer and the SHA-256 of every patch applied to it. The script's
# docstring has the reasoning; the short version is that an image digest
# says "the same", not "what", and two of the revisions are movable tags.
#
# /mcuhome/workspace.json, not inside the workspace: a file inside a layer
# makes that layer dirty and shows up in `git status`, and a file at the
# topdir would be shadowed the moment a backend bind-mounts a writable
# view over it. /mcuhome/ is the namespace the build-container contract
# already reserves for us (§2.2).
COPY containers/builder/workspace-record.py /tmp/workspace-record.py
RUN python3 /tmp/workspace-record.py \
        --topdir /mcuhome/workspace \
        --output /mcuhome/workspace.json \
        --clone narrow-depth-1 \
        --patch "zephyr=/tmp/patches/zephyr-${ZEPHYR_REVISION}-nrf53-spinel-stack.patch" \
        --patch "chip=/tmp/patches/connectedhomeip-${CHIP_REVISION}-vanilla-zephyr.patch"

# The manifest repository's directory is emptied and left in place. west
# needed west.yml to get here; the SDK mount brings it back at run time,
# because ADR 0018 makes the SDK a hash-pinned package fetched per build
# rather than image content.
#
# It is re-read on EVERY CMake configure — zephyr/scripts/zephyr_module.py
# calls west's Manifest.from_file(), which reads <topdir>/<manifest.path>/
# west.yml off the filesystem — so a missing mount is not a subtle
# problem. It stops the build at configure time with "MalformedConfig:
# file not found: manifest file /mcuhome/workspace/mcuhome/west.yml",
# which names the exact path nobody mounted. That is the right failure
# and it needs no help from us.
RUN rm -rf /mcuhome/workspace/mcuhome && mkdir /mcuhome/workspace/mcuhome

# What this image answers to `describe`, written by the program that
# answers it (§2.2's optional static self-description, r7). The file has
# to hold "exactly what a describe invocation answers", and the strongest
# way to keep that true is to generate it from one: this file IS the
# program's answer, so there is no second implementation of the `program`
# block to drift away from the first.
#
# Last in this stage on purpose. `trees` is read out of
# /mcuhome/workspace.json, which the step above writes, so an answer
# computed any earlier would describe an image that does not exist yet.
#
# The program body is BORROWED from the build context and never
# installed: only /mcuhome leaves this stage, and PYTHONPATH points
# outside it. An image carrying a copy of the SDK would contradict ADR
# 0018, which makes the SDK a hash-pinned package fetched per build and
# mounted — and would leave a second, older program body inside the image
# for a mounted one to disagree with.
#
# The invocation is §5.1's, unabridged — action plus the absolute path of
# a request document, and the answer where that document says. Anything
# other than exit 0 stops the image build here, which is the right place
# to find out that this image cannot describe itself.
COPY mcuhome /tmp/program/mcuhome
RUN printf '%s\n' '{"request": 1, "result": "/tmp/describe.json"}' > /tmp/describe-request.json \
 && PYTHONPATH=/tmp/program /opt/west-venv/bin/python3 \
        -m mcuhome.compiler.abi describe /tmp/describe-request.json \
 && install -m 0644 /tmp/describe.json /mcuhome/describe.json

# --------------------------------------------------------------------------
# Stage 3: the image itself
# --------------------------------------------------------------------------
FROM ${DEBIAN_IMAGE}

ARG ZEPHYR_SDK_VERSION

LABEL org.opencontainers.image.title="MCUHome builder" \
      org.opencontainers.image.description="Build environment for MCUHome firmware: Zephyr SDK, west, gn, zap, ccache." \
      org.opencontainers.image.source="https://github.com/mcu-home/mcuhome" \
      org.opencontainers.image.licenses="Apache-2.0"

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Three groups, kept apart so the reason for each package survives:
#
#   build system   what Zephyr's CMake glue calls out to, plus the host
#                  compiler native_sim (and therefore twister) builds with
#   runtime        git for Zephyr's version stamping, ccache because the
#                  design makes it a hard requirement, xz/unzip/file for
#                  the odd unpacking step in a module's CMake
#   electron       zap is a prebuilt Electron binary and links against the
#                  desktop stack even when it never opens a window; the
#                  list is the Depends: field of the upstream zap .deb
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        cmake \
        device-tree-compiler \
        g++ \
        gcc \
        gperf \
        libc6-dev \
        make \
        ninja-build \
        python3 \
        python3-venv \
        ca-certificates \
        ccache \
        file \
        git \
        unzip \
        xz-utils \
        libatspi2.0-0t64 \
        libgtk-3-0t64 \
        libnotify4 \
        libnss3 \
        libsecret-1-0 \
        libuuid1 \
        libxss1 \
        libxtst6 \
 && rm -rf /var/lib/apt/lists/*

# The Python environment, pinned down to the transitive dependency (see
# requirements.txt). A virtualenv rather than the system interpreter so
# the image never fights Debian's PEP 668 marker, and so `python3` inside
# a build means one thing.
COPY containers/builder/requirements.txt /opt/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /opt/requirements.txt

COPY --from=fetch /opt/zephyr-sdk-${ZEPHYR_SDK_VERSION} /opt/zephyr-sdk-${ZEPHYR_SDK_VERSION}
COPY --from=fetch /opt/gn /opt/gn
COPY --from=fetch /opt/zap /opt/zap

# The baked west workspace, the record of what it is, and since r7 the
# static `describe` answer computed from that record. One COPY: the whole
# /mcuhome namespace is ours (build-container-contract.md §2.2), and
# nothing else in the image writes there.
COPY --from=workspace /mcuhome /mcuhome

# The program of the build-container contract (§2.2): a fixed absolute
# path, never looked up on PATH, and a thin launcher over mcuhome.compiler.abi.
#
# 0755 rather than the mode git happens to carry, because §2.2 requires it
# to be executable by EVERY user the backend may exec as — the backend
# runs the program as the calling user where it can (the `--user` argument
# in mcuhome/compiler/container.py), and that user owns nothing in this image and
# has no /etc/passwd entry here. Set with a RUN rather than with COPY
# --chmod so the classic builder can build this image too.
#
# After the COPY above, not before: that one lands the whole /mcuhome
# namespace, and this file has to survive it.
#
COPY containers/builder/run /mcuhome/run
RUN chmod 0755 /mcuhome/run

# The labels of §2.1 — "pre-start scheduling data": they let a backend
# pick an image before paying for a container start, and they are NOT
# authoritative about what the program can do; `describe` is (§7.1).
#
# `org.mcuhome.contract=1` claims conformance with contract v1, and the
# claim became true in three steps, none of them here: the program
# implements all three actions of §7 (`describe`, `verify`, `build` —
# reviewed adversarially, exercised against this very image), and §4's
# D1 erratum sanctions the one fixed path this image depends on, the
# SDK mount point that `describe` declares in `program.trees`. The
# program's *body* still arrives with the SDK mount — what the image
# guarantees is the launcher, the workspace the actions run against,
# and the toolchain, which is exactly what the three labels name.
#
# Values, not ARGs, on purpose: the Zephyr line is pinned by west.yml
# and baked into the workspace above, so a label that could drift from
# the baked reality via a --build-arg would be scheduling data lying
# about the image. Bump them together with the pins, like the tag.
#
# All three spellings were wrong until r7, and each was wrong in a way
# that made the label unusable rather than merely untidy — §2.1.1 fixes
# the grammar precisely because a constraint is *evaluated* against these
# values:
#
#   the NAME was org.mcuhome.model.toolchain — a casualty of ADR 0020's
#       mcuhome.toolchain -> mcuhome.model.toolchain rename, which walked
#       into a label the contract owns. A backend looks for
#       org.mcuhome.toolchain (§2.1) and finds nothing, and "a container
#       that does not carry a named label does not qualify": this image
#       could satisfy no toolchain constraint at all.
#   the ZEPHYR value was v4.4.0 — §2.1.1 asks for the version "**without**
#       the leading `v` west uses: `4.4.0` for the `v4.4.0` this project
#       pins", and the leading v is outside the value range as well, so no
#       version comparison could be made against it.
#   the TOOLCHAIN value was zephyr-sdk-1.0.1/arm-zephyr-eabi — a `/` that
#       the character class does not admit, in a value whose shape is
#       `<identity>-<version>`. The target triple moves out of the value
#       rather than to the end of it: §2.1.1 names this image's value
#       itself, `zephyr-sdk-<version>`, and the version part is what an
#       SDK release compares, so a triple after the final hyphen would
#       leave nothing numeric to compare. The triple is not lost — it is
#       the ZEPHYR_TOOLCHAIN ARG at the top of this file, which is where
#       an image input belongs.
LABEL org.mcuhome.contract="1" \
      org.mcuhome.zephyr="4.4.0" \
      org.mcuhome.toolchain="zephyr-sdk-1.0.1"

# Git refuses to operate in a repository owned by another user (the
# CVE-2022-24765 hardening), and this is exactly that situation: the
# workspace above is baked as root, while the container runs as the
# calling user's UID. Both things a build reads out of .git go through
# git, and they fail differently:
#
#   west's `git ls-tree manifest-rev west.yml`, resolving the zephyr
#       `import:` — LOUD. west raises ManifestImportFailed, zephyr_module.py
#       exits, CMake stops with FATAL_ERROR.
#   Zephyr's `git describe`, stamping BUILD_VERSION — SILENT.
#       cmake/modules/git.cmake prints a STATUS line and simply leaves the
#       variable unset; the banner loses its version and the firmware is a
#       third size, with no error anywhere.
#
# So this line is not a convenience. Scoped to the prefix rather than the
# global '*': the glob matches nested repositories too, submodules of the
# Matter SDK included (verified with GIT_TEST_ASSUME_DIFFERENT_OWNER=1,
# git 2.55.0). --system, because the caller's UID has no home directory
# here and a per-user config would never be read.
RUN git config --system --add safe.directory '/mcuhome/workspace/*'

# ccache configuration belongs to the image, not to every invocation: the
# system-wide file is what a bare `docker run <image> ccache -s` sees too,
# which is how the cache is inspected. CCACHE_DIR is a bind mount the
# runner supplies (mcuhome/compiler/container.py) so the cache outlives the
# container.
#
#   compiler_check = content   the compilers here never change without the
#                              image tag changing, and mtimes (the default
#                              check) do not survive a `docker pull`.
#   ignore_options = -specs=*  without this, ccache caches NOTHING for a
#                              Zephyr build. Zephyr's picolibc integration
#                              passes `-specs=picolibc.specs`, a bare file
#                              name the linker resolves against the
#                              toolchain; ccache resolves it against the
#                              working directory, fails to stat it, and
#                              gives up on every single compile with
#                              "bad compiler arguments" (measured: 1077 of
#                              1077 compiles, 0 cacheable). Excluding the
#                              option from the hash is safe here for the
#                              same reason as compiler_check above: the
#                              specs file is part of the toolchain, and the
#                              toolchain is part of the image tag.
#
# `temporary_dir` is deliberately NOT set: ccache's default puts it inside
# CCACHE_DIR, which is the bind mount — so the final rename of a cached
# object stays on one filesystem, and the inode cache (which lives there)
# actually works. Pointing it at the container's own /tmp costs a
# cross-device copy per object and silently disables that cache.
RUN mkdir -p /ccache \
 && chmod 1777 /ccache \
 && printf '%s\n' \
        'max_size = 5.0G' \
        'compiler_check = content' \
        'ignore_options = -specs=*' \
        > /etc/ccache.conf

# The container runs as the calling user's UID/GID (no root-owned build
# directories on the host), which usually has no /etc/passwd entry — so
# HOME has to be somewhere world-writable. The runner sets it as well;
# this default is what makes a bare `docker run <image> <tool>` work.
RUN mkdir -p /tmp/mcuhome-home && chmod 1777 /tmp/mcuhome-home

ENV PATH=/opt/venv/bin:/opt/gn:/opt/zap:/usr/local/bin:/usr/bin:/bin \
    VIRTUAL_ENV=/opt/venv \
    HOME=/tmp/mcuhome-home \
    ZEPHYR_TOOLCHAIN_VARIANT=zephyr \
    ZEPHYR_SDK_INSTALL_DIR=/opt/zephyr-sdk-${ZEPHYR_SDK_VERSION} \
    ZAP_INSTALL_PATH=/opt/zap \
    CCACHE_DIR=/ccache

# No ENTRYPOINT: the image is invoked with a full command line (`west
# build …`, `west twister …`, `ccache -s`) by the builder, by CI and by
# hand, and an entrypoint would only get in the way of all three.
CMD ["/bin/bash"]
