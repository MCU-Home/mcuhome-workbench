# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Named builders: where a build runs, as configuration.

A **builder** is configuration *about* a build target, never a third
target: the target vocabulary underneath (``local``/``remote``), its
validation and its typed refusals stay
:mod:`mcuhome.workbench.buildmethods`'s. What this module adds is the
product shape on top — ``mcuhome device build`` should simply work, and
*where* it built is something the user configured once:

* ``local`` — a build on this machine, in the mode ``build.mode``
  names; nothing required, the container image is optionally
  configurable.
* ``remote`` — a build server; the address is required, and the
  credentials live **next to the other secrets**, in
  ``secrets/builder/<name>.yaml``, never in the builder map itself —
  configuration files are committed, secrets are not.

Builders are configured as a map under the area ``builder``, keyed by
the builder's name::

    builder:
      attic:
        target: remote
        server: 10.0.0.5:8291

The key is the name, so a file cannot define one builder twice and a
name cannot disagree with the entry it heads. Entries merge **by name**
across the configuration layers; on a name collision the layer nearer
the project wins whole — no per-field merging of one builder from two
layers, because half a builder from ``/etc`` and half from
``mcuhome.yaml`` is a deployment nobody wrote. ``build.builder`` names
the builder a plain build uses; selection itself (explicit name, then
that key, then the target ``build.target`` names) is
:func:`select_builder`, and the token lookup walks the same nearest-wins
ladder the definition did.

The credential file is deliberately tolerant of keys it does not know:
today it carries ``token``, later it can grow certificate or
TLS-pinning material when the session protocol does — an unknown key
there is the future, not a typo worth refusing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location

from mcuhome.workbench.buildtarget import BUILD_TARGETS, DEFAULT_BUILD_TARGET, TARGET_REMOTE

__all__ = [
    "CREDENTIALS_TOKEN_KEY",
    "Builder",
    "SelectedBuilder",
    "merge_builders",
    "parse_builders",
    "select_builder",
]

#: The one key a ``secrets/builder/<name>.yaml`` carries today.
CREDENTIALS_TOKEN_KEY = "token"

#: A builder's name becomes a file name
#: (``secrets/builder/<name>.yaml``), so it is restricted the way device
#: names are: lowercase, digits, dashes — nothing a path or a shell
#: reinterprets.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Which keys an entry accepts beyond ``target``, per target, and which
#: of those are required. The single source the refusals quote.
_TARGET_KEYS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # target: (required, optional)
    "local": ((), ("container_image",)),
    "remote": (("server",), ()),
}


@dataclass(frozen=True)
class Builder:
    """One named builder, and where it was defined.

    *origin* and *source* are resolution facts in the words every other
    resolved value uses — the layer, and the file inside it — and they
    are carried on the value itself because merge-by-name makes them
    per-builder: two builders of one resolved map may come from two
    different files, and ``mcuhome config print`` owes the user both
    answers. Neither is a key a file may write: a file that could state
    its own layer could forge its own precedence.
    """

    name: str
    #: One of :data:`~mcuhome.workbench.buildtarget.BUILD_TARGETS`:
    #: where a build at this builder runs.
    target: str
    #: The configuration layer that defined this builder (after the
    #: merge: the nearest one that did).
    origin: str
    #: The file it was defined in.
    source: str
    #: ``remote``: the build server's address, ``IP/hostname[:port]``.
    server: str | None = None
    #: ``local``: the container image to build in, in the four pin
    #: forms; ``None`` searches the configured repositories.
    container_image: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, every declared key present.

        A key a value does not have is ``null`` rather than absent: a
        client that renders this table should not have to ask whether a
        missing key means "unset" or "this version does not know it".
        """
        return {
            "name": self.name,
            "target": self.target,
            "origin": self.origin,
            "source": self.source,
            "server": self.server,
            "container_image": self.container_image,
        }


def _entry_location(entry: Any, file: Path, name: str) -> Location:
    try:
        line, column = entry.lc.line, entry.lc.col
        return Location(file=file, line=line + 1, column=column + 1, key=f"builder.{name}")
    except AttributeError:
        return Location(file=file, key=f"builder.{name}")


def parse_builders(value: Any, *, file: Path, origin: str) -> tuple[Builder, ...]:
    """One layer's ``builder:`` map, validated entry by entry.

    The key is the builder's name, which is why nothing here checks for
    a duplicate: a mapping cannot hold one name twice, and two *files*
    that define the same name are the merge's business.
    """
    if not isinstance(value, dict):
        raise ConfigError(
            "The option 'builder' must be a map of builders, keyed by name.",
            location=Location(file=file, key="builder"),
            hint=(
                "one section per builder, for example:\n"
                "    builder:\n"
                "      attic:\n"
                "        target: remote\n"
                "        server: 10.0.0.5:8291"
            ),
        )
    parsed: list[Builder] = []
    for name, entry in value.items():
        location = _entry_location(entry, file, str(name))
        _check_name(name, location=location)
        if not isinstance(entry, dict):
            raise ConfigError(
                f'The builder "{name}" must be a mapping of `option: value` pairs.',
                location=location,
                hint=(
                    "it takes target:, and what that target needs:\n"
                    f"    {name}:\n      target: local"
                ),
            )
        builder = _parse_entry(str(name), dict(entry), location=location, origin=origin, file=file)
        parsed.append(builder)
    return tuple(parsed)


def _check_name(name: Any, *, location: Location) -> None:
    if not isinstance(name, str) or not _NAME_RE.match(name):
        shown = name if isinstance(name, str) else repr(name)
        raise ConfigError(
            f"{shown!r} is not a usable builder name.",
            location=location,
            hint=(
                "use lowercase letters, digits and dashes, starting with a letter or "
                "digit — the name becomes the credentials file "
                "secrets/builder/<name>.yaml"
            ),
        )


def _parse_entry(name: str, entry: dict, *, location: Location, origin: str, file: Path) -> Builder:
    target = entry.get("target")
    if target not in BUILD_TARGETS:
        raise ConfigError(
            f'The builder "{name}" names no target.'
            if target is None
            else f'"{target}" is not a build target MCUHome knows.',
            location=location,
            hint=(
                "the build targets are "
                + ", ".join(BUILD_TARGETS)
                + ": local compiles in a build environment on this machine, "
                "and remote on a build server"
            ),
        )
    required, optional = _TARGET_KEYS[target]
    allowed = {"target", *required, *optional}
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ConfigError(
            f'The builder "{name}" has no option called {unknown[0]!r}.',
            location=location,
            hint=(
                f"a {target} builder takes: "
                + (", ".join((*required, *optional)) or "nothing beyond target")
                + ". Credentials never go here — they live in "
                f"secrets/builder/{name}.yaml."
            ),
        )
    for key in required:
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise ConfigError(
                f'The builder "{name}" is missing its {key}.',
                location=location,
                hint=_REQUIRED_HINTS[target],
            )
    values: dict[str, Any] = {}
    for key in (*required, *optional):
        raw = entry.get(key)
        if raw is None:
            continue
        if not isinstance(raw, str) or not raw:
            raise ConfigError(
                f'The builder "{name}" has a {key} that is not a string.',
                location=location,
                hint=_REQUIRED_HINTS[target],
            )
        values[key] = raw
    return Builder(
        name=name,
        target=target,
        origin=origin,
        source=str(file),
        server=values.get("server"),
        container_image=values.get("container_image"),
    )


_REQUIRED_HINTS = {
    "local": (
        "a local builder needs nothing beyond its target; `container_image:` "
        "optionally pins the image it builds in"
    ),
    "remote": "a remote builder names its build server:\n"
    "    builder:\n"
    "      attic:\n"
    "        target: remote\n"
    "        server: 10.0.0.5:8291\n"
    "  (the token goes to secrets/builder/<name>.yaml, never here)",
}


def merge_builders(lower: tuple[Builder, ...], upper: tuple[Builder, ...]) -> tuple[Builder, ...]:
    """Merged by name: the nearer layer wins whole.

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

    ``builder`` is ``None`` exactly for the fallback — no ``--builder``
    and no ``build.builder``, so a plain build at the target
    ``build.target`` names, with every default.
    """

    #: One of :data:`~mcuhome.workbench.buildtarget.BUILD_TARGETS`:
    #: where the selected builder builds.
    target: str
    builder: Builder | None = None
    server: str | None = None
    token: str | None = None
    container_image: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready — and the token is in no document, ever."""
        return {
            "target": self.target,
            "builder": None if self.builder is None else self.builder.to_dict(),
            "server": self.server,
            "container_image": self.container_image,
        }


def select_builder(
    builders: Sequence[Builder],
    *,
    name: str | None,
    default: str | None,
    token_of: Callable[[Builder], str | None],
    fallback: str = DEFAULT_BUILD_TARGET,
) -> SelectedBuilder:
    """The selection ladder, below the caller that names a target outright.

    A caller that states the target itself (``--build-target`` plus its
    target-specific flags) bypasses the builder map entirely and never
    reaches this function. Here: an explicit *name* first, then the
    configured *default*, then *fallback* — the target ``build.target``
    names, which is this machine unless somebody moved it. *token_of*
    answers a remote builder's credentials — a lookup the caller owns,
    because where the secrets directories are is layer knowledge, not
    vocabulary.
    """
    chosen: Builder | None = None
    if name is not None:
        chosen = _named(builders, name, selector='--builder "{0}"')
    elif default is not None:
        chosen = _named(builders, default, selector='build.builder "{0}"')
    if chosen is None:
        return SelectedBuilder(target=fallback or DEFAULT_BUILD_TARGET)
    return SelectedBuilder(
        target=chosen.target,
        builder=chosen,
        server=chosen.server,
        token=token_of(chosen) if chosen.target == TARGET_REMOTE else None,
        container_image=chosen.container_image,
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
            "`builder:` in mcuhome.yaml (or your user/system configuration.yaml), "
            "or name the target outright with --build-target."
        ),
    )
