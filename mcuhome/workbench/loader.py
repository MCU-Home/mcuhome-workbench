# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Pipeline stage 1: YAML parsing and ``!secret`` resolution.

The parser is ruamel.yaml in round-trip mode for exactly one reason: it
keeps line and column information on every mapping and sequence, and the
whole validation layer is built around pointing at the offending line
(yaml-schema.md §10, builder-pipeline.md §1.5).

``!secret name`` reads ``name`` from the tree's ``secrets.yaml``
(yaml-schema.md §9, deliberately ESPHome-identical UX). Resolution
happens here, before validation, so no later stage ever sees a secret
reference — and an unknown secret is reported with the line of the
``!secret`` tag, not of the file it should have been in.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML, YAMLError

from mcuhome.model.errors import ConfigError, Location

__all__ = ["SecretRef", "load_config", "load_yaml_file", "resolve_secrets"]


@dataclass(frozen=True)
class SecretRef:
    """An unresolved ``!secret`` reference, with the position of its tag."""

    name: str
    line: int
    column: int

    def location(self, file: Path, key: str | None = None) -> Location:
        return Location(file=file, line=self.line, column=self.column, key=key)


def _secret_constructor(constructor: Any, node: Any) -> SecretRef:
    del constructor
    return SecretRef(
        name=str(node.value),
        line=node.start_mark.line + 1,
        column=node.start_mark.column + 1,
    )


def _yaml() -> YAML:
    yaml = YAML(typ="rt")
    yaml.constructor.add_constructor("!secret", _secret_constructor)
    return yaml


def load_yaml_file(path: Path) -> Any:
    """Parse one YAML file, turning parser failures into config errors."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(
            f'The configuration file "{path}" does not exist.',
            location=Location(file=path),
            hint="check the path, or create the file",
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f'The configuration file "{path}" could not be read: {exc.strerror}.',
            location=Location(file=path),
        ) from exc

    try:
        return _yaml().load(text)
    except YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or "the file is not valid YAML"
        raise ConfigError(
            f"This file is not valid YAML: {problem}.",
            location=Location(
                file=path,
                line=(mark.line + 1) if mark is not None else None,
                column=(mark.column + 1) if mark is not None else None,
            ),
            hint=(
                "YAML is indentation-sensitive: check that the line above is indented "
                "with spaces (never tabs) and that every key ends with a colon"
            ),
        ) from exc


def _load_secrets(
    secrets_file: Path, ref: SecretRef, file: Path, key: str | None
) -> dict[str, Any]:
    if not secrets_file.is_file():
        raise ConfigError(
            f'This configuration uses the secret "{ref.name}", but there is no '
            f"{secrets_file.name} to read it from.",
            location=ref.location(file, key),
            hint=(f"create {secrets_file} with a line like:\n    {ref.name}: your-value-here"),
        )
    data = load_yaml_file(secrets_file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(
            f"{secrets_file.name} must be a list of `name: value` pairs.",
            location=Location(file=secrets_file, line=1, column=1),
            hint=f"write one secret per line, for example:\n    {ref.name}: your-value-here",
        )
    return dict(data)


def resolve_secrets(data: Any, *, file: Path, secrets_file: Path) -> Any:
    """Replace every :class:`SecretRef` in *data* with its value.

    The secrets file is read at most once, and only when the config
    actually uses a secret.
    """
    secrets: dict[str, Any] | None = None

    def walk(value: Any, key_path: str) -> Any:
        nonlocal secrets
        if isinstance(value, SecretRef):
            if secrets is None:
                secrets = _load_secrets(secrets_file, value, file, key_path)
            if value.name not in secrets:
                known = ", ".join(sorted(secrets)) if secrets else "none"
                raise ConfigError(
                    f'There is no secret called "{value.name}" in {secrets_file.name}.',
                    location=value.location(file, key_path),
                    hint=(
                        f"add it to {secrets_file}:\n"
                        f"    {value.name}: your-value-here\n"
                        f"  (secrets currently defined: {known})"
                    ),
                )
            return secrets[value.name]
        if isinstance(value, dict):
            for item_key in list(value.keys()):
                child = f"{key_path}.{item_key}" if key_path else str(item_key)
                value[item_key] = walk(value[item_key], child)
            return value
        if isinstance(value, list):
            for index in range(len(value)):
                value[index] = walk(value[index], f"{key_path}[{index}]")
            return value
        return value

    return walk(data, "")


def load_config(entry: Path, *, secrets_file: Path) -> Any:
    """Stage 1: parse *entry* and resolve its secrets."""
    data = load_yaml_file(entry)
    if data is None:
        raise ConfigError(
            "This device configuration is empty.",
            location=Location(file=entry, line=1, column=1),
            hint=(
                "a device configuration needs at least a device: section, for example:\n"
                "    device:\n"
                "      name: my-sensor\n"
                "      board: nrf7002dk/nrf5340/cpuapp"
            ),
        )
    if not isinstance(data, dict):
        raise ConfigError(
            "This device configuration must be a mapping of sections.",
            location=Location(file=entry, line=1, column=1),
            hint="the top level holds the sections device:, network:, hardware:, node:",
        )
    return resolve_secrets(data, file=entry, secrets_file=secrets_file)
