# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The supported programmatic surface of the MCUHome builder.

**This module is the API. Everything else is an implementation detail.**
Names exported here are covered by the project's SemVer promise (ADR
0005): they do not change shape within a major version, and a breaking
change to one of them is a breaking change to the builder. Names anywhere
else in the package — including :mod:`mcuhome.cli`, which is a command
line and not a library — may move between releases without notice, and a
caller that imports them is on its own.

The intended consumer is a program that embeds the builder rather than
running it: the MCUHome dashboard imports it in-process (dashboard ADR
0011 decision 1) so that a configuration error arrives in an editor's
gutter as a marker with a fix hint rather than in a log pane as a line of
text. The dependency has exactly one direction — the dashboard declares
the builder versions it supports and follows the builder's releases; the
builder never learns that a dashboard exists.

What is here, in the order a caller needs it:

``open_config_tree`` / ``find_device``
    Where the configuration lives and which file is a given device's.
``load_model``
    Stages 1-3 on one device, raising on the first thing that is wrong.
``read_model``
    A canonical model back from JSON — the other end of the wire. A build
    server receives one of these and starts at stage 4; it never sees the
    configuration tree and never sees a secrets file (dashboard ADR 0007
    decision 4). ``mcuhome build --model <file>`` is the same thing as a
    command.
``validate_device``
    The same three stages, returning **every** problem as typed errors
    instead of raising — one pass, all markers.
``error_dicts`` / ``ConfigError.to_dict``
    Those errors as plain dictionaries: message, file (relative to the
    configuration tree), line, column, key, hint, kind.
``registry_data`` / ``config_json_schema``
    What the builder knows about hardware and Matter, and the shape of
    ``main.yaml``, as data an editor or a picker can consume.
``read_manifest``
    ``build-manifest.json`` of a finished build.

Everything here is synchronous and CPU-bound (YAML parsing, mostly). A
caller with an event loop runs it in an executor.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome import __version__
from mcuhome.errors import (
    BuildError,
    ConfigError,
    ConfigErrorGroup,
    GenerationError,
    Location,
    MCUHomeError,
    error_dicts,
)
from mcuhome.export import config_json_schema, registry_data
from mcuhome.loader import load_config
from mcuhome.manifest import MANIFEST_FILE, read_manifest
from mcuhome.model import MODEL_VERSION, DeviceModel
from mcuhome.resolve import resolve
from mcuhome.schema import parse_config
from mcuhome.tree import (
    DEVICE_ENTRY,
    DEVICES_DIR,
    SECRETS_FILE,
    ConfigTree,
    find_config_root,
    is_config_root,
    open_tree,
    resolve_device,
)
from mcuhome.validate import validate

__all__ = [
    "DEVICES_DIR",
    "DEVICE_ENTRY",
    "MANIFEST_FILE",
    "MODEL_VERSION",
    "SECRETS_FILE",
    "VERSION",
    "BuildError",
    "ConfigError",
    "ConfigErrorGroup",
    "ConfigTree",
    "DeviceModel",
    "GenerationError",
    "Location",
    "MCUHomeError",
    "ValidationResult",
    "config_json_schema",
    "error_dicts",
    "find_config_root",
    "find_device",
    "is_config_root",
    "load_model",
    "open_config_tree",
    "read_manifest",
    "read_model",
    "registry_data",
    "validate_device",
]

#: The builder's version, for a consumer that declares a supported range.
VERSION = __version__


def open_config_tree(root: Path | None = None, *, cwd: Path | None = None) -> ConfigTree:
    """Resolve a configuration tree from an explicit root or by discovery.

    Raises :class:`ConfigError` when *root* is not a configuration tree,
    or when discovery from *cwd* upwards finds none.
    """
    return open_tree(root, cwd=cwd)


def find_device(
    spec: str, *, config_root: Path | None = None, cwd: Path | None = None
) -> tuple[ConfigTree, Path]:
    """Resolve a device name or path to its tree and its entry file.

    The same resolution the CLI's ``<device>`` argument gets: a folder
    name under ``devices/``, or an explicit path to a device folder or a
    YAML file.
    """
    return resolve_device(spec, config_root=config_root, cwd=cwd)


def load_model(entry: Path, *, tree: ConfigTree) -> DeviceModel:
    """Run stages 1-3 on one device configuration: load, validate, resolve.

    The result is the canonical device model (builder-pipeline.md §1.2) —
    the single representation between YAML and every generator, and the
    wire format of a remote build. Raises :class:`ConfigError` for a
    single problem and :class:`ConfigErrorGroup` when validation found
    several; :func:`validate_device` is the same work without the raise.
    """
    data = load_config(entry, secrets_file=tree.secrets_file)
    config = parse_config(data, file=entry)
    validate(config)
    return resolve(config)


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


@dataclass(frozen=True)
class ValidationResult:
    """The outcome of checking one configuration, problems and all."""

    entry: Path
    tree: ConfigTree
    #: The resolved model, or None when the configuration was rejected.
    model: DeviceModel | None
    #: Every problem found, in file order. Empty exactly when *model* is
    #: not None. Almost always :class:`ConfigError` instances — the wider
    #: type is for the errors that have no place in a file to point at,
    #: which are still user-facing messages and still serialize the same
    #: way.
    errors: tuple[MCUHomeError, ...]

    @property
    def ok(self) -> bool:
        return self.model is not None

    def error_dicts(self) -> list[dict[str, Any]]:
        """The problems as dictionaries, with paths relative to the tree."""
        return [error.to_dict(root=self.tree.root) for error in self.errors]

    def raise_errors(self) -> None:
        """Raise what :func:`validate_device` caught, for a caller that wants it.

        The bridge back to the raising style: a command line renders an
        exception, a program inspects a result, and both should be able
        to use the same call. Does nothing when there is nothing wrong.
        """
        if not self.errors:
            return
        if len(self.errors) == 1:
            raise self.errors[0]
        raise ConfigErrorGroup([error for error in self.errors if isinstance(error, ConfigError)])

    def to_dict(self) -> dict[str, Any]:
        """The whole result as JSON-ready data — what ``--json`` prints."""
        return {
            "ok": self.ok,
            "file": _relative(self.entry, self.tree.root),
            "errors": self.error_dicts(),
            "model": None if self.model is None else self.model.to_dict(),
        }


def _relative(path: Path, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve()))
    except (ValueError, OSError):
        return str(path)


def validate_device(entry: Path, *, tree: ConfigTree) -> ValidationResult:
    """Stages 1-3, reporting every problem instead of raising.

    Validation deliberately does not stop at the first error, and this is
    the entry point that keeps it that way for a caller: one pass over
    the configuration, every marker at once. Errors arrive as the builder
    raised them — typed, located, with their fix hints — so a caller can
    render them or serialize them with
    :meth:`~mcuhome.errors.ConfigError.to_dict`.

    An error the builder raised *without* a location (a missing secrets
    file, say) is reported the same way, because to the person reading it
    the difference does not matter.
    """
    try:
        model = load_model(entry, tree=tree)
    except ConfigErrorGroup as group:
        return ValidationResult(entry=entry, tree=tree, model=None, errors=tuple(group.errors))
    except MCUHomeError as error:
        return ValidationResult(entry=entry, tree=tree, model=None, errors=(error,))
    return ValidationResult(entry=entry, tree=tree, model=model, errors=())
