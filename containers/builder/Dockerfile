# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
#
# The MCUHome builder image (ADR 0007): the single build environment used
# by developers, CI and the Home Assistant add-on. It contains TOOLS ONLY
# — never MCUHome sources and never a west workspace. The workspace is
# bind-mounted at run time, at the same absolute path it has on the host,
# so a build directory is usable from inside and outside the container
# (mcuhome/container.py assembles the invocation).
#
# Versioned in lockstep with the Zephyr pin in west.yml: the tag is
# `zephyr-<zephyr version>-r<image revision>`, and the single source of
# truth for it is mcuhome/container.py. Bump the revision whenever
# anything below changes; bump the Zephyr part when west.yml does.
#
# Build (from the repository root, the context is this directory only):
#     docker build -t ghcr.io/mcu-home/builder:zephyr-4.4.0-r1 containers/builder
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
# Stage 2: the image itself
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
COPY requirements.txt /opt/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check --upgrade pip \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check -r /opt/requirements.txt

COPY --from=fetch /opt/zephyr-sdk-${ZEPHYR_SDK_VERSION} /opt/zephyr-sdk-${ZEPHYR_SDK_VERSION}
COPY --from=fetch /opt/gn /opt/gn
COPY --from=fetch /opt/zap /opt/zap

# ccache configuration belongs to the image, not to every invocation: the
# system-wide file is what a bare `docker run <image> ccache -s` sees too,
# which is how the cache is inspected. CCACHE_DIR is a bind mount the
# runner supplies (mcuhome/container.py) so the cache outlives the
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
