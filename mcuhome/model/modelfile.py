# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Reading a canonical device model back from its JSON form.

This lives in ``mcuhome.model`` and not next to the pipeline that
*produces* the model, and the location is the point. The model is the
wire format between the two sides of the build-container contract, and
the container side runs on what §6.1 promises it: the runtime the SDK
package declares — a bare Python interpreter, stdlib and nothing else.
Every module the SDK entry point imports, transitively, must hold to
that, which is exactly the constraint this package is built under
("dependency-free by construction", ADR 0020) and exactly the
constraint ``mcuhome.workbench`` is not: its supported surface parses
YAML, so importing it pulls a third-party parser onto a path that only
ever reads JSON. ``tests_py/test_container_closure.py`` pins the
consequence — the entry point's import closure stays stdlib plus
``mcuhome``.

``mcuhome.workbench.api`` re-exports :func:`read_model` unchanged; for
everything host-side, the supported surface is still the place to get
it.
"""

from __future__ import annotations

import json
from pathlib import Path

from mcuhome.model import __version__ as VERSION
from mcuhome.model.errors import BuildError
from mcuhome.model.model import MODEL_VERSION, DeviceModel

__all__ = ["read_model"]


def read_model(path: Path) -> DeviceModel:
    """A canonical device model back from its JSON form.

    The receiving end of builder-pipeline.md §6: the model is the wire
    format of a remote build, so a build server is handed one of these and
    runs stages 4 and 5 on it. It deliberately re-runs nothing — the
    configuration tree, the secrets file and the whole front half of the
    pipeline stay on the machine that owns them (dashboard ADR 0007
    decision 4), which is also why a build server never needs to be
    trusted with them.

    Refuses in plain language, and the refusals are the interesting part:

    * a file that is not JSON, or not an object;
    * a ``model_version`` this builder does not implement — named on both
      sides, never silently coerced. A newer model may describe things
      this builder has no generator for, and an older one may mean
      something different by a field that still parses;
    * a model missing a field this version requires.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the device model {path}: {error.strerror}.",
            hint=(
                "a device model is the JSON mcuhome build writes next to the "
                "application it generates (device-model.json), and what "
                "mcuhome validate --json carries in its `model` field."
            ),
        ) from error
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise BuildError(
            f"The device model {path} is not valid JSON ({error.msg}, line {error.lineno}).",
            hint="it is builder output — regenerate it rather than editing it",
        ) from error
    if not isinstance(data, dict):
        raise BuildError(
            f"The device model {path} does not describe a device.",
            hint="it is builder output — regenerate it rather than editing it",
        )

    found = data.get("model_version")
    if found != MODEL_VERSION:
        raise BuildError(
            f"The device model {path} is version {found!r}, and this builder "
            f"implements version {MODEL_VERSION}.",
            hint=(
                "the canonical model is a versioned contract (dashboard ADR 0007 "
                "decision 4): a mismatch is a refusal that names both numbers, "
                f"never a guess. Build with a builder that implements model "
                f"version {found!r}, or regenerate the model with this one "
                f"(MCUHome {VERSION})."
            ),
        )
    try:
        return DeviceModel.from_dict(data)
    except (KeyError, TypeError, ValueError) as error:
        raise BuildError(
            f"The device model {path} is missing something this builder needs: {error}.",
            hint=(
                "it states model version "
                f"{MODEL_VERSION}, so this is a truncated or hand-edited file "
                "rather than a version mismatch. Regenerate it."
            ),
        ) from error
