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
===========================  ======================================

Stages 4 (code generation) and 5 (container build) are not implemented
yet; ``mcuhome build`` refuses accordingly.
"""

__version__ = "0.1.0.dev0"
