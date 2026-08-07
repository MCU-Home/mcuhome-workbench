# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""MCUHome firmware builder.

Compiles MCUHome YAML device configurations into Zephyr firmware images.

Pipeline (builder-pipeline.md §3), by module:

===========================  ======================================
:mod:`mcuhome.tree`          find the configuration tree and device
:mod:`mcuhome.loader`        stage 1: YAML parse, ``!secret``
:mod:`mcuhome.schema`        stage 2a: shape, as typed raw config
:mod:`mcuhome.validate`      stage 2b: cross-refs, gates, conformance
:mod:`mcuhome.resolve`       stage 3: defaults and completion
:mod:`mcuhome.model`         the canonical device model
:mod:`mcuhome.generate`      stage 4: the per-device build tree
:mod:`mcuhome.container`     stage 5: compile in the builder image
:mod:`mcuhome.workspace`     stage 5: compile on the host (``--native``)
===========================  ======================================
"""

__version__ = "0.1.0.dev0"
