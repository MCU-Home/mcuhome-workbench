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
ESPHome-shaped UX; the ladder is the project layout's — commissioning
identity per device, shared values project-wide).
Resolution happens here, before validation, so no later stage ever sees
a secret reference — and an unknown secret is reported with the line of
the ``!secret`` tag, not of the file it should have been in. Reading the
secrets file runs the secrets-hygiene permission check: a file other
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
secret extends its secrets-hygiene permission check to ``value.path``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location
from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.constructor import RoundTripConstructor

from mcuhome.workbench.diagnostics import Diagnostic
from mcuhome.workbench.project import DEVICES_DIR, require_secret_file

__all__ = [
    "FileRef",
    "SecretRef",
    "device_secrets_file",
    "editing_yaml",
    "in_project_layout",
    "load_config",
    "read_editable_yaml",
    "read_yaml_file",
    "require_folder_name",
    "resolve_secrets",
    "secret_references",
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
    """A parser that knows this package's two tags, and nothing else does.

    The subclass is the point. ``add_constructor`` is a **class** method
    in ruamel: calling it on the stock round-trip constructor registers
    the tag on every round-trip parser in the process — this package's
    own editing parser (:func:`editing_yaml`), and any other library
    reading YAML in the same program. Two things follow from that, and
    both are wrong: a document somebody else parses would suddenly
    resolve ``!secret``, and ``!file`` — which is bound to the file being
    read here — would resolve *their* relative paths against the last
    device file this package happened to open.

    So every parse gets a constructor class of its own, and the
    registrations die with it. The representer of :func:`editing_yaml` is
    deliberately not treated the same way: :class:`FileRef` is this
    package's own type, and a dump that writes it back as the reference
    it is, is right wherever it happens.
    """
    yaml = YAML(typ="rt")
    yaml.Constructor = type("_ConfigConstructor", (RoundTripConstructor,), {})
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

    Preserving quotes is the other half of "writing back": a round trip
    is a promise about the whole file, and a value the user wrote as
    ``'keep me'`` that comes back as ``keep me`` is an edit they did not
    ask for, in a line they did not touch.
    """
    yaml = YAML(typ="rt")
    yaml.preserve_quotes = True
    yaml.representer.add_representer(FileRef, _fileref_representer)
    return yaml


def _not_valid_yaml(exc: YAMLError, path: Path) -> ConfigError:
    """The parser's complaint, at the line it happened on."""
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or "the file is not valid YAML"
    return ConfigError(
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
    )


def _text_of(path: Path) -> str:
    """The file's text, with the two ways of not having one said in words."""
    try:
        return path.read_text(encoding="utf-8")
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


def read_editable_yaml(path: Path) -> Any:
    """Parse one YAML file the way a writer of it has to: tags stay tags.

    The counterpart of :func:`read_yaml_file` for a caller that is about
    to *edit* the file, or that only wants to know which keys are in it:
    the round-trip parser keeps comments, order and unknown tags, and no
    ``!file`` is followed. The second half is the point wherever the file
    holds key material — reading the name of an entry must not read the
    private key it points at — and it is what makes a write through
    :func:`editing_yaml` reproduce the reference instead of the content.

    The value of a tagged entry is therefore ruamel's own
    ``TaggedScalar``, not a :class:`FileRef`: it round-trips, and a
    caller that means to answer a value refuses it rather than printing
    a path as if it were one.
    """
    text = _text_of(path)
    try:
        return editing_yaml().load(text)
    except YAMLError as exc:
        raise _not_valid_yaml(exc, path) from exc


def secret_references(data: Any) -> tuple[str, ...]:
    """Every ``!secret`` name *data* refers to, in the order they appear.

    Reads a parsed device configuration — :func:`read_yaml_file`'s
    answer, before :func:`resolve_secrets` has replaced anything — and
    answers the names, deduplicated. That is what makes "which devices
    use this secret" a fact rather than a guess: the list comes from the
    same references a build resolves.
    """
    found: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, SecretRef):
            if value.name not in found:
                found.append(value.name)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return tuple(found)


def read_yaml_file(path: Path) -> Any:
    """Parse one YAML file, turning parser failures into config errors."""
    text = _text_of(path)
    try:
        return _yaml(path).load(text)
    except YAMLError as exc:
        raise _not_valid_yaml(exc, path) from exc


def in_project_layout(entry: Path, *, secrets_file: Path) -> bool:
    """Whether *entry* is a device of the project ``secrets_file`` belongs to.

    The one thing that separates the two kinds of device file this
    package reads: one that lives at ``<project>/devices/<device>/`` is a
    device *of* a project, with everything the project keys on its
    folder, and one anywhere else is a file somebody handed over —
    ``mcuhome device validate ./example.yaml`` — whose directory stands
    in for a project that does not exist.
    """
    return entry.parent.parent == secrets_file.parent.parent / DEVICES_DIR


def require_folder_name(data: Any, *, entry: Path, secrets_file: Path) -> None:
    """A device of a project is named by its folder, and says so itself.

    The folder is the identity: the build directory, the build lock, the
    result documents, the OTA image, the per-device secrets, the pairing
    credentials and the patches a build carries are all keyed on it. A
    file that claims a different ``device.name`` would make the same
    device two devices, each answering to a different half of that list —
    so it is refused here, once, rather than reconciled differently by
    every surface that asks.

    Only inside a project. A bare file somebody points at has no folder
    that means anything (:func:`in_project_layout`), and its own name is
    all there is.
    """
    if not in_project_layout(entry, secrets_file=secrets_file):
        return
    device = data.get("device") if isinstance(data, dict) else None
    stated = device.get("name") if isinstance(device, dict) else None
    folder = entry.parent.name
    if not isinstance(stated, str) or not stated or stated == folder:
        return
    raise ConfigError(
        f'This device lives in the folder "{folder}" and calls itself "{stated}".',
        location=_key_location(device, "name", entry=entry),
        hint=(
            f"a device is one name: the folder and device.name are the same word, because "
            f"everything MCUHome writes for a device — its build directory, its secrets, "
            f"its pairing credentials, the patches it is built with — is keyed on it. "
            f"Either rename the folder:\n"
            f"    mv devices/{folder} devices/{stated}\n"
            f"  or write the folder's name in the file:\n"
            f"    device:\n"
            f"      name: {folder}"
        ),
    )


def _key_location(mapping: Any, key: str, *, entry: Path) -> Location:
    """Where *key* is written in *mapping*, as far as the parser knows."""
    lc = getattr(mapping, "lc", None)
    path = f"device.{key}"
    if lc is not None:
        try:
            line, column = lc.key(key)
        except (KeyError, TypeError):  # pragma: no cover - defensive
            pass
        else:
            return Location(file=entry, line=line + 1, column=column + 1, key=path)
    return Location(file=entry, key=path)


def device_secrets_file(secrets_file: Path, data: Any, entry: Path) -> Path:
    """``secrets/devices/<device>.yaml``, next to the project's main secrets file.

    The per-device secrets file of the project layout —
    where ``mcuhome device matter-pairing`` puts a device's commissioning
    values. For a device inside the project layout the name is the
    device *folder's*, never the configuration's own ``device.name``
    claim: the folder is the identity every project surface keys on, and
    a ``device.name`` that disagrees must not let one device read — or
    overwrite — another device's credentials. Such a file is refused
    before this is asked (:func:`require_folder_name`); keying on the
    folder here is what makes that refusal the only way the two can
    differ. Only a bare file outside the layout answers with its
    ``device.name`` (folder name standing in); its stand-in root holds no
    second device to collide with.
    """
    parent = entry.parent
    if in_project_layout(entry, secrets_file=secrets_file):
        return secrets_file.parent / "devices" / f"{parent.name}.yaml"
    device = data.get("device") if isinstance(data, dict) else None
    name = device.get("name") if isinstance(device, dict) else None
    if not isinstance(name, str) or not name:
        name = parent.name
    return secrets_file.parent / "devices" / f"{name}.yaml"


def _read_secret_file(
    secrets_file: Path,
    ref: SecretRef,
    on_warning: Callable[[Diagnostic], None] | None,
) -> dict[str, Any]:
    require_secret_file(secrets_file, key_material=False, on_warning=on_warning)
    data = read_yaml_file(secrets_file)
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
    on_warning: Callable[[Diagnostic], None] | None,
) -> dict[str, Any]:
    """Every secret this configuration may name, device values winning.

    The ladder: the device's own
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
    on_warning: Callable[[Diagnostic], None] | None = None,
) -> Any:
    """Replace every :class:`SecretRef` in *data* with its value.

    The secrets file is read at most once, and only when the config
    actually uses a secret — so the secrets-hygiene permission check
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
    on_warning: Callable[[Diagnostic], None] | None = None,
) -> Any:
    """Stage 1: parse *entry* and resolve its secrets."""
    data = read_yaml_file(entry)
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
    # Before a secret is read: a file that cannot say which device it is
    # must not send this package looking for that device's secrets.
    require_folder_name(data, entry=entry, secrets_file=secrets_file)
    return resolve_secrets(data, file=entry, secrets_file=secrets_file, on_warning=on_warning)
