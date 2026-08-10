# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The shared vocabulary: what MCUHome's parties must agree about.

The device model and its JSON form, the registry (boards, drivers,
clusters, partitions, update schemes), the context and build-manifest
formats including the frozen ID rule of ADR 0018 §6, the OTA version
arithmetic, the commissioning credentials and the curve under them, the
error types, and the file hash both formats and the invocation ABI share.
**No build machinery** — nothing here compiles, generates, or drives a
west workspace, and nothing here reads the process it runs in.

It is the package every execution site needs and none of them may
re-derive (ADR 0020 decision 1). Most sharply the context ID: a build
server recomputes it from bytes it received (ADR 0019 §8) while carrying
no build logic at all (decision 4), and the entire purpose of that value
is that independent parties arrive at the same one.

==============================  ===============================================
:mod:`mcuhome.model.model`      the canonical device model
:mod:`mcuhome.model.registry`   the static tables everything validates against
:mod:`mcuhome.model.context`    the remote-build context format (ADR 0018)
:mod:`mcuhome.model.artifacts`  one declared artifact, as every method reports it
:mod:`mcuhome.model.manifest`   ``build-manifest.json``, as a format
:mod:`mcuhome.model.ota`        the device version, and what derives from it
:mod:`mcuhome.model.pairing`    commissioning credentials, as one atomic group
:mod:`mcuhome.model.p256`       the curve arithmetic pairing and signing share
:mod:`mcuhome.model.export`     the registry, as data
:mod:`mcuhome.model.toolchain`  Zephyr line and blob resolution (ADR 0013)
:mod:`mcuhome.model.hashes`     the one file hash both sides of §3.3 compute
:mod:`mcuhome.model.userpaths`  per-user directories, from a given environment
:mod:`mcuhome.model.errors`     the error type and its plain-language rendering
==============================  ===============================================
"""

#: The version of the whole release, and the only place it is written
#: down. ADR 0017 §3 and ADR 0020 decision 8 give ``mcuhome-model``,
#: ``mcuhome-workbench`` and ``mcuhome-compiler`` one shared version, one
#: tag and one release; this package is the root of the dependency chain,
#: so it is where that number lives. The three ``pyproject.toml`` files
#: under ``packaging/`` all read it from here (``dynamic = ["version"]``),
#: and no second copy exists to disagree with it — which is why nothing
#: re-exports it under a name of its own either.
__version__ = "0.1.0.dev0"
