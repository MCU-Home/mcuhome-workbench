# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a build: stages 1-3, the context, the build methods, signing.

Everything between a YAML file and something a compiler can be handed:
finding the project directory, resolving its configuration, parsing a
device file, validating it, resolving it into a device model, creating
the build context of ADR 0018, and the three build methods of ADR 0020
decision 6 — ``local-dev``, ``local``, ``remote`` — behind one
interface. Client-side signing lives here too (ADR 0015 §8), because
the private key belongs to the person driving the build and to nobody
the build talks to.

This is what runs wherever a build is *driven* rather than performed: the
command line, the dashboard, a third-party embedder. It must never carry
a toolchain (ADR 0017 §2), which is why stages 4-5 are
:mod:`mcuhome.compiler` and reached through a build container that
already has them.

=====================================  ============================================
:mod:`mcuhome.workbench.api`           the supported surface — import this
:mod:`mcuhome.workbench.project`       the project directory: marker, layout, init
:mod:`mcuhome.workbench.configuration` the five-layer option model (ADR 0022)
:mod:`mcuhome.workbench.builders`      named builders and their selection (ADR 0023)
:mod:`mcuhome.workbench.loader`        stage 1: YAML parse, ``!secret``
:mod:`mcuhome.workbench.schema`        stage 2a: shape, as typed raw config
:mod:`mcuhome.workbench.validate`      stage 2b: cross-refs, gates, conformance
:mod:`mcuhome.workbench.resolve`       stage 3: defaults and completion
:mod:`mcuhome.workbench.contextdir`    creating and verifying a context directory
:mod:`mcuhome.workbench.buildmethods`  the three build methods, behind one call
:mod:`mcuhome.workbench.sessionclient` the ``remote`` method's protocol client
:mod:`mcuhome.workbench.signing`       the per-project firmware signing key
:mod:`mcuhome.workbench.imgtool`       signing an image afterwards (ADR 0015 §8)
:mod:`mcuhome.workbench.otafile`       wrapping a signed image in a Matter ``.ota``
:mod:`mcuhome.workbench.provision`     drawing a device's commissioning credentials
:mod:`mcuhome.workbench.scaffold`      a new device's first configuration file
:mod:`mcuhome.workbench.configschema`  the JSON Schema for ``main.yaml``
=====================================  ============================================

**Importing this package from another program?** Use
:mod:`mcuhome.workbench.api` and nothing else. It is the supported
surface and the only part covered by the project's SemVer promise
(ADR 0005); every other module here may change shape between releases.
"""

#: The workbench's own version since the ADR 0024 repo split — no longer
#: read from mcuhome.model, which versions with the SDK repository. From
#: v1.0 the two are coupled at major.minor (~=X.Y.0 edges).
__version__ = "0.1.0.dev0"
