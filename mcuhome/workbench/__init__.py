# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Driving a build: stages 1-3, the context, the build itself, signing.

Everything between a YAML file and something a compiler can be handed:
finding the project directory, resolving its configuration, parsing a
device file, validating it, resolving it into a device model, creating
the build context, and the build targets
— ``local``, ``remote`` — behind one interface. Client-side
signing lives here too, because the private key belongs to
the person driving the build and to nobody the build talks to.

This is what runs wherever a build is *driven* rather than performed: the
command line, the dashboard, a third-party embedder. It must never carry
a toolchain, which is why stages 4-5 are
:mod:`mcuhome.compiler` and reached through the build environment that
already has them.

**Importing this package from another program?** Use
:mod:`mcuhome.workbench.api` and nothing else: it is the supported
surface, ``docs/api.md`` in this repository is its reference, and every
other module here may change shape between releases without notice.
"""

#: The workbench's own version since the repository split — no longer
#: read from mcuhome.model, which versions with the SDK repository. From
#: v1.0 the two are coupled at major.minor (~=X.Y.0 edges).
__version__ = "0.1.0.dev0"
