# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Named builders: where a build runs, as configuration (ADR 0023).

A **builder** is configuration *about* a build method, never a fourth
method: the method vocabulary underneath (``local``/``local-dev``/
``remote``), its validation and its typed refusals stay
:mod:`mcuhome.workbench.buildmethods`'s (ADR 0020). What this module
adds is the product shape on top — ``mcuhome device build`` should
simply work, and *where* it built is something the user configured
once:

* ``local`` — a build container on this machine; nothing required,
  the image is optionally configurable.
* ``local-dev`` — the caller's own west workspace and host toolchain;
  the workspace path is required, because only the user knows it.
* ``remote`` — a build server; the address is required, and the
  credentials live **next to the other secrets**, in
  ``secrets/build-server/<name>.yaml`` (ADR 0022 §5), never in the
  builder list itself — configuration files are committed, secrets
  are not.

Builder lists merge **by name** across the configuration layers; on a
name collision the layer nearer the project wins whole — no per-field
merging of one builder from two layers, because half a builder from
``/etc`` and half from ``mcuhome.yaml`` is a deployment nobody wrote.
``default_builder`` names the builder a plain build uses; selection
itself (explicit name, then the default, then the built-in ``local``
fallback) is :func:`select_builder`, and the token lookup walks the
same nearest-wins ladder the definition did.

The credential file is deliberately tolerant of keys it does not know:
today it carries ``token``, later it can grow certificate or
TLS-pinning material when the session protocol does (ADR 0023 §4) —
an unknown key there is the future, not a typo worth refusing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location

__all__ = [
    "BUILDER_TYPES",
    "CREDENTIALS_TOKEN_KEY",
    "Builder",
    "SelectedBuilder",
    "merge_builders",
    "parse_builders",
    "select_builder",
]

#: The builder types, one per build method (ADR 0023 §1).
BUILDER_TYPES = ("local", "local-dev", "remote")

#: The one key a ``secrets/build-server/<name>.yaml`` carries today.
CREDENTIALS_TOKEN_KEY = "token"

#: A builder's name becomes a file name (``secrets/build-server/
#: <name>.yaml``), so it is restricted the way device names are:
#: lowercase, digits, dashes — nothing a path or a shell reinterprets.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Which keys each type accepts beyond ``name`` and ``type``, and which
#: of those are required. The single source the refusals quote.
_TYPE_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # type: (required, optional)
    "local": ((), ("image",)),
    "local-dev": (("workspace",), ()),
    "remote": (("server",), ()),
}


@dataclass(frozen=True)
class Builder:
    """One named builder, and where it was defined.

    *layer* and *source* are resolution facts, carried on the value
    itself because merge-by-name makes them per-builder: two builders
    of one resolved list may come from two different files, and
    ``mcuhome config print`` owes the user both answers.
    """

    name: str
    type: str
    #: The configuration layer that defined this builder (after the
    #: merge: the nearest one that did).
    layer: str
    #: The file it was defined in.
    source: str
    #: ``remote``: the build server's address, ``IP/hostname[:port]``.
    server: str | None = None
    #: ``local-dev``: the west workspace to compile in.
    workspace: Path | None = None
    #: ``local``: the build-container reference; ``None`` takes the
    #: default image.
    image: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, for ``mcuhome config print``."""
        data: dict[str, Any] = {"name": self.name, "type": self.type, "layer": self.layer}
        if self.server is not None:
            data["server"] = self.server
        if self.workspace is not None:
            data["workspace"] = str(self.workspace)
        if self.image is not None:
            data["image"] = self.image
        return data


def _entry_location(entry: Any, file: Path, index: int) -> Location:
    try:
        line, column = entry.lc.line, entry.lc.col
        return Location(file=file, line=line + 1, column=column + 1)
    except AttributeError:
        return Location(file=file, key=f"builders[{index}]")


def parse_builders(
    value: Any,
    *,
    file: Path,
    origin: str,
    expand_path: Callable[[str], Path],
) -> tuple[Builder, ...]:
    """One layer's ``builders:`` list, validated entry by entry.

    *expand_path* is the file layer's path rule (``~`` from the stated
    environment, relative to the defining file) — owned by the caller
    so this module states no path policy of its own.
    """
    if not isinstance(value, list):
        raise ConfigError(
            "The option 'builders' must be a list of builder entries.",
            location=Location(file=file, key="builders"),
            hint=(
                "one entry per builder, for example:\n"
                "    builders:\n"
                "      - name: attic\n"
                "        type: remote\n"
                "        server: 10.0.0.5:8291"
            ),
        )
    parsed: list[Builder] = []
    seen: dict[str, int] = {}
    for index, entry in enumerate(value):
        location = _entry_location(entry, file, index)
        if not isinstance(entry, dict):
            raise ConfigError(
                "Every builder entry must be a mapping with name: and type:.",
                location=location,
                hint="start each entry with `- name: <builder-name>`",
            )
        builder = _parse_entry(dict(entry), location=location, origin=origin, file=file)
        builder = _typed(builder, dict(entry), location=location, expand_path=expand_path)
        if builder.name in seen:
            raise ConfigError(
                f'This file defines the builder "{builder.name}" twice.',
                location=location,
                hint=(
                    "within one file every builder name is unique; two *files* may "
                    "define the same name — the layer nearer the project wins"
                ),
            )
        seen[builder.name] = index
        parsed.append(builder)
    return tuple(parsed)


def _parse_entry(entry: dict, *, location: Location, origin: str, file: Path) -> Builder:
    name = entry.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        shown = name if isinstance(name, str) else repr(name)
        raise ConfigError(
            f"{shown!r} is not a usable builder name."
            if name is not None
            else "This builder entry has no name.",
            location=location,
            hint=(
                "use lowercase letters, digits and dashes, starting with a letter or "
                "digit — the name becomes the credentials file "
                "secrets/build-server/<name>.yaml"
            ),
        )
    kind = entry.get("type")
    if kind not in BUILDER_TYPES:
        raise ConfigError(
            f'The builder "{name}" names no known type.'
            if kind is None
            else f'"{kind}" is not a builder type MCUHome knows.',
            location=location,
            hint=(
                "the builder types are "
                + ", ".join(BUILDER_TYPES)
                + ": local compiles in a build container on this machine, "
                "local-dev in your own west workspace, and remote on a build server"
            ),
        )
    return Builder(name=name, type=kind, layer=origin, source=str(file))


def _typed(
    builder: Builder,
    entry: dict,
    *,
    location: Location,
    expand_path: Callable[[str], Path],
) -> Builder:
    required, optional = _TYPE_KEYS[builder.type]
    allowed = {"name", "type", *required, *optional}
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ConfigError(
            f'The builder "{builder.name}" has no option called {unknown[0]!r}.',
            location=location,
            hint=(
                f"a {builder.type} builder takes: "
                + (", ".join((*required, *optional)) or "nothing beyond name and type")
                + ". Credentials never go here — they live in "
                f"secrets/build-server/{builder.name}.yaml."
            ),
        )
    for key in required:
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise ConfigError(
                f'The builder "{builder.name}" is missing its {key}.',
                location=location,
                hint=_REQUIRED_HINTS[builder.type],
            )
    values: dict[str, Any] = {}
    for key in (*required, *optional):
        raw = entry.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw:
            raise ConfigError(
                f'The builder "{builder.name}" has a {key} that is not a string.',
                location=location,
                hint=_REQUIRED_HINTS[builder.type],
            )
        values[key] = expand_path(raw) if key == "workspace" else raw
    return Builder(
        name=builder.name,
        type=builder.type,
        layer=builder.layer,
        source=builder.source,
        server=values.get("server"),
        workspace=values.get("workspace"),
        image=values.get("image"),
    )


_REQUIRED_HINTS = {
    "local": "a local builder needs nothing; `image:` optionally names a build container",
    "local-dev": "a local-dev builder compiles in your own west workspace:\n"
    "    - name: bench\n"
    "      type: local-dev\n"
    "      workspace: ~/zephyr-workspace",
    "remote": "a remote builder names its build server:\n"
    "    - name: attic\n"
    "      type: remote\n"
    "      server: 10.0.0.5:8291\n"
    "  (the token goes to secrets/build-server/<name>.yaml, never here)",
}


def merge_builders(lower: tuple[Builder, ...], upper: tuple[Builder, ...]) -> tuple[Builder, ...]:
    """ADR 0023 §3: by name, the nearer layer wins whole.

    Order is kept readable rather than clever: the lower layer's order,
    with a replaced builder staying in its place and genuinely new
    names appended in their own order.
    """
    replacing = {builder.name: builder for builder in upper}
    merged = [replacing.pop(builder.name, builder) for builder in lower]
    merged.extend(builder for builder in upper if builder.name in replacing)
    return tuple(merged)


@dataclass(frozen=True)
class SelectedBuilder:
    """What a build call needs to know after selection.

    ``builder`` is ``None`` exactly for the built-in fallback — no
    ``--builder``, no ``default_builder``, so a plain ``local`` build
    with every default (ADR 0023: today's default stays ``local``).
    """

    #: One of :data:`~mcuhome.workbench.buildmethods.METHODS` — a
    #: builder *type* is a method name, on purpose.
    method: str
    builder: Builder | None = None
    server: str | None = None
    token: str | None = None
    workspace: Path | None = None
    image: str | None = None


def select_builder(
    builders: Sequence[Builder],
    *,
    name: str | None,
    default: str | None,
    token_of: Callable[[Builder], str | None],
) -> SelectedBuilder:
    """The selection ladder of ADR 0023 §2, below the fully manual rung.

    The manual rung (``--build-mode`` plus its mode-specific flags)
    bypasses the builder list entirely and never reaches this function.
    Here: an explicit *name* first, then the configured *default*, then
    the built-in ``local`` fallback. *token_of* answers a remote
    builder's credentials — a lookup the caller owns, because where the
    secrets directories are is layer knowledge, not vocabulary.
    """
    chosen: Builder | None = None
    if name is not None:
        chosen = _named(builders, name, selector='--builder "{0}"')
    elif default is not None:
        chosen = _named(builders, default, selector='default_builder "{0}"')
    if chosen is None:
        return SelectedBuilder(method="local")
    return SelectedBuilder(
        method=chosen.type,
        builder=chosen,
        server=chosen.server,
        token=token_of(chosen) if chosen.type == "remote" else None,
        workspace=chosen.workspace,
        image=chosen.image,
    )


def _named(builders: Sequence[Builder], name: str, *, selector: str) -> Builder:
    for builder in builders:
        if builder.name == name:
            return builder
    known = ", ".join(builder.name for builder in builders) or "none are defined"
    raise ConfigError(
        f"{selector.format(name)} names no configured builder.",
        hint=(
            f"builders configured on this machine: {known}. Define one under "
            "`builders:` in mcuhome.yaml (or your user/system configuration.yaml), "
            "or build fully manually with --build-mode."
        ),
    )
