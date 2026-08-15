# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Pipeline stage 1: YAML parsing, ``!secret`` and ``!file`` resolution.

The parser is ruamel.yaml in round-trip mode for exactly one reason: it
keeps line and column information on every mapping and sequence, and the
whole validation layer is built around pointing at the offending line
(yaml-schema.md §10, builder-pipeline.md §1.5).

``!secret name`` reads ``name`` from the device's own
``secrets/devices/<name>.yaml`` first and the project-wide
``secrets/main.yaml`` second (yaml-schema.md §9, deliberately
ESPHome-shaped UX; the ladder is ADR 0022's layout, PO 2026-08-15 —
commissioning identity per device, shared values project-wide).
Resolution happens here, before validation, so no later stage ever sees
a secret reference — and an unknown secret is reported with the line of
the ``!secret`` tag, not of the file it should have been in. Reading the
secrets file runs the permission check of ADR 0022 §5: a file other
users can reach draws a warning through the caller's *on_warning*.

``!file path`` makes a value out of an external file (PO 2026-08-14):
the value **is** the file's raw content — a :class:`FileRef`, a plain
``str`` to every consumer — and the file itself stays reachable as
``value.path`` for the consumer that must hand a *file* to an external
tool (imgtool's ``--key`` is the founding case; whatever comes next gets
the same two answers for free). Deliberately not ``!include``: in the
Home Assistant world that tag means "parse and inline YAML", and this
one means "the bytes of that file, verbatim". Resolution is eager and
strict — a relative path resolves against the referencing YAML file's
directory, ``path`` is always the real absolute path, and a file that
does not exist or cannot be read is a located refusal at load time,
before any value is consumed. A consumer that treats such a value as a
secret extends its ADR 0022 §5 permission check to ``value.path``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location
from ruamel.yaml import YAML, YAMLError

from mcuhome.workbench.project import DEVICES_DIR, check_secret_file

__all__ = [
    "FileRef",
    "SecretRef",
    "device_secrets_file",
    "editing_yaml",
    "load_config",
    "load_yaml_file",
    "resolve_secrets",
]


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


class FileRef(str):
    """The raw content of a ``!file``-referenced file, and the file itself.

    A plain ``str`` to every consumer that wants the value (equality,
    hashing and serialization are the content's), so ``!file`` composes
    with existing readers without a code change. The two extras carry
    what the tag adds:

    - :attr:`path` — the referenced file as a **real absolute path**
      (symlinks resolved), safe to hand to an external tool from any
      working directory.
    - :attr:`raw` — the reference exactly as the YAML spelled it, so a
      round-trip write (:func:`editing_yaml`) reproduces ``!file <raw>``
      instead of spilling the content into the document.
    """

    __slots__ = ("path", "raw")

    path: Path
    raw: str

    def __new__(cls, content: str, *, path: Path, raw: str) -> FileRef:
        ref = super().__new__(cls, content)
        ref.path = path
        ref.raw = raw
        return ref


def _file_constructor(file: Path) -> Callable[[Any, Any], FileRef]:
    """The ``!file`` constructor, bound to the YAML file being parsed."""

    def construct(constructor: Any, node: Any) -> FileRef:
        del constructor
        location = Location(
            file=file, line=node.start_mark.line + 1, column=node.start_mark.column + 1
        )
        if node.id != "scalar" or not str(node.value).strip():
            raise ConfigError(
                "!file needs a file path.",
                location=location,
                hint="reference the file whose content this value is:\n    key: !file name.pem",
            )
        raw = str(node.value).strip()
        if raw.startswith("~"):
            raise ConfigError(
                f'"{raw}" starts with `~`, which !file does not expand.',
                location=location,
                hint=(
                    "a configuration file answers for itself, independent of who "
                    "reads it — write the path relative to this file, or absolute"
                ),
            )
        target = Path(raw)
        if not target.is_absolute():
            target = file.parent / target
        target = target.resolve()
        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ConfigError(
                f'The file "{raw}" referenced here does not exist (looked at {target}).',
                location=location,
                hint=(
                    "a !file reference resolves relative to the file that contains "
                    "it; create the file, or fix the path"
                ),
            ) from exc
        except (OSError, UnicodeDecodeError) as exc:
            reason = getattr(exc, "strerror", None) or "it is not a text file"
            raise ConfigError(
                f'The file "{raw}" referenced here cannot be read: {reason}.',
                location=location,
            ) from exc
        return FileRef(content, path=target, raw=raw)

    return construct


def _yaml(path: Path) -> YAML:
    yaml = YAML(typ="rt")
    yaml.constructor.add_constructor("!secret", _secret_constructor)
    yaml.constructor.add_constructor("!file", _file_constructor(path))
    return yaml


def _fileref_representer(representer: Any, ref: FileRef) -> Any:
    return representer.represent_scalar("!file", ref.raw)


def editing_yaml() -> YAML:
    """A round-trip YAML instance for writing loaded data back.

    The one wrinkle it exists for: a loaded document may hold
    :class:`FileRef` values, and a plain dump would write their *content*
    where the ``!file`` reference stood — replacing a pointer to a secret
    with the secret. This instance writes every ``FileRef`` back as
    ``!file <raw>``, byte-for-byte the reference the user wrote.
    """
    yaml = YAML(typ="rt")
    yaml.representer.add_representer(FileRef, _fileref_representer)
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
        return _yaml(path).load(text)
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


def device_secrets_file(secrets_file: Path, data: Any, entry: Path) -> Path:
    """``secrets/devices/<name>.yaml``, next to the project's main secrets file.

    The per-device secrets file of ADR 0022's layout (PO 2026-08-15) —
    where ``mcuhome device matter-pairing`` puts a device's commissioning
    values. For a device inside the project layout the name is the
    device *folder's*, never the configuration's own ``device.name``
    claim: the folder is the identity every project surface keys on, and
    a ``device.name`` that disagrees must not let one device read — or
    overwrite — another device's credentials (review 2026-08-15). Only a
    bare file outside the layout answers with its ``device.name``
    (folder name standing in); its stand-in root holds no second device
    to collide with.
    """
    parent = entry.parent
    if parent.parent == secrets_file.parent.parent / DEVICES_DIR:
        return secrets_file.parent / "devices" / f"{parent.name}.yaml"
    device = data.get("device") if isinstance(data, dict) else None
    name = device.get("name") if isinstance(device, dict) else None
    if not isinstance(name, str) or not name:
        name = parent.name
    return secrets_file.parent / "devices" / f"{name}.yaml"


def _read_secret_file(
    secrets_file: Path,
    ref: SecretRef,
    on_warning: Callable[[str], None] | None,
) -> dict[str, Any]:
    check_secret_file(secrets_file, key_material=False, on_warning=on_warning)
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


def _load_secrets(
    device_file: Path,
    secrets_file: Path,
    ref: SecretRef,
    file: Path,
    key: str | None,
    on_warning: Callable[[str], None] | None,
) -> dict[str, Any]:
    """Every secret this configuration may name, device values winning.

    The ladder (ADR 0022, PO 2026-08-15): the device's own
    ``secrets/devices/<name>.yaml`` answers first — its commissioning
    identity lives there — and the project-wide ``secrets/main.yaml``
    answers for everything shared between devices (a WiFi password).
    Lookup is per name, so one configuration can read from both.
    """
    sources = [source for source in (secrets_file, device_file) if source.is_file()]
    if not sources:
        raise ConfigError(
            f'This configuration uses the secret "{ref.name}", but there is no '
            f"{secrets_file.name} to read it from.",
            location=ref.location(file, key),
            hint=(f"create {secrets_file} with a line like:\n    {ref.name}: your-value-here"),
        )
    merged: dict[str, Any] = {}
    for source in sources:  # main first, device last: the device file wins
        merged.update(_read_secret_file(source, ref, on_warning))
    return merged


def resolve_secrets(
    data: Any,
    *,
    file: Path,
    secrets_file: Path,
    on_warning: Callable[[str], None] | None = None,
) -> Any:
    """Replace every :class:`SecretRef` in *data* with its value.

    The secrets file is read at most once, and only when the config
    actually uses a secret — so the permission check of ADR 0022 §5
    runs exactly when the file's content is actually about to be used.
    """
    secrets: dict[str, Any] | None = None
    device_file = device_secrets_file(secrets_file, data, entry=file)

    def walk(value: Any, key_path: str) -> Any:
        nonlocal secrets
        if isinstance(value, SecretRef):
            if secrets is None:
                secrets = _load_secrets(
                    device_file, secrets_file, value, file, key_path, on_warning
                )
            if value.name not in secrets:
                known = ", ".join(sorted(secrets)) if secrets else "none"
                looked = secrets_file.name
                if device_file.is_file():
                    looked = f"{device_file.parent.name}/{device_file.name} or {secrets_file.name}"
                raise ConfigError(
                    f'There is no secret called "{value.name}" in {looked}.',
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


def load_config(
    entry: Path,
    *,
    secrets_file: Path,
    on_warning: Callable[[str], None] | None = None,
) -> Any:
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
    return resolve_secrets(data, file=entry, secrets_file=secrets_file, on_warning=on_warning)
