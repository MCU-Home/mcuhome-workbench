# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Error types for the MCUHome builder.

Validation errors are user interface, not developer diagnostics
(builder-pipeline.md §9: "validation error messages are tested, because
they are UX"). Every error therefore answers three questions in this
order:

1. **What is wrong** — one plain sentence, no jargon the config author
   did not write themselves.
2. **Where** — file, line, column and the dotted key path.
3. **What to do** — a concrete fix, ideally copy-pasteable.

Rendered shape (stable; asserted by the test suite)::

    Error: Board "nrf99dk" is not supported by MCUHome yet.
      in main.yaml, line 5, column 10 (device.board)
      Fix: use one of the boards MCUHome supports today: nrf7002dk/nrf5340/cpuapp

A user must never see a Python traceback: the CLI catches everything
derived from :class:`MCUHomeError` and prints the rendering above.

**Serialized shape (stable; part of the public API of** :mod:`mcuhome.api`
**).** :meth:`ConfigError.to_dict` answers the same three questions as
fields rather than as prose, for a caller that puts them in an editor's
gutter instead of on a terminal::

    {"message": ..., "file": ..., "line": ..., "column": ...,
     "key": ..., "hint": ..., "kind": ...}

``file`` is rendered **relative to the configuration tree** when a root
is given, because that is how an editor addresses a file and because an
absolute server-side path is more than a browser needs to know. ``kind``
is the error class name, so a consumer can tell "your configuration is
wrong" from "the build could not start" without parsing the message.
:func:`error_dicts` flattens whatever was raised — one error or a whole
:class:`ConfigErrorGroup` — into a list of them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "BuildError",
    "ConfigError",
    "ConfigErrorGroup",
    "ErrorCollector",
    "GenerationError",
    "Location",
    "MCUHomeError",
    "error_dicts",
]


def _display_path(path: Path) -> str:
    """Render *path* relative to the working directory when that is shorter."""
    try:
        relative = os.path.relpath(path, Path.cwd())
    except ValueError:  # different drive on Windows
        return str(path)
    return relative if len(relative) < len(str(path)) else str(path)


@dataclass(frozen=True)
class Location:
    """Where in a config file something went wrong.

    ``line``/``column`` are 1-based (what an editor shows). ``key`` is the
    dotted path of the offending key, e.g. ``device.board`` or
    ``node.endpoints[0].clusters.temperature_measurement.source``.
    """

    file: Path | None = None
    line: int | None = None
    column: int | None = None
    key: str | None = None

    def with_key(self, key: str) -> Location:
        return Location(file=self.file, line=self.line, column=self.column, key=key)

    def describe(self) -> str:
        """Human-readable "where", without the leading ``in``."""
        parts: list[str] = []
        if self.file is not None:
            parts.append(_display_path(self.file))
        if self.line is not None:
            parts.append(f"line {self.line}")
        if self.column is not None:
            parts.append(f"column {self.column}")
        where = ", ".join(parts)
        if self.key:
            where = f"{where} ({self.key})" if where else self.key
        return where

    def sort_key(self) -> tuple[str, int, int]:
        return (str(self.file or ""), self.line or 0, self.column or 0)

    def relative_file(self, root: Path | None = None) -> str | None:
        """:attr:`file` as the configuration tree addresses it.

        Inside *root* this is the tree-relative path — what an editor
        opens a buffer by, and all a caller on the other side of a
        network connection has any business learning about the server's
        filesystem. Outside it (or without a root) the path is rendered
        as it is, because a wrong-looking absolute path is still more
        useful than ``None``.
        """
        if self.file is None:
            return None
        if root is not None:
            try:
                return str(Path(self.file).resolve().relative_to(Path(root).resolve()))
            except (ValueError, OSError):
                pass
        return str(self.file)


class MCUHomeError(Exception):
    """Base class for every error the CLI is allowed to show to a user."""

    def render(self) -> str:  # pragma: no cover - overridden everywhere
        return str(self)

    def to_dict(self, *, root: Path | None = None) -> dict[str, Any]:
        """This error as the serialized shape of the module docstring.

        The base implementation is the "no location" case: an error the
        builder raised without a configuration file to point at. It still
        carries a message a user is meant to read, which is the whole
        difference between this and a traceback.
        """
        del root  # nothing to make relative
        return {
            "message": str(self),
            "file": None,
            "line": None,
            "column": None,
            "key": None,
            "hint": None,
            "kind": type(self).__name__,
        }

    def to_dicts(self, *, root: Path | None = None) -> list[dict[str, Any]]:
        """Every problem this exception carries, as dictionaries."""
        return [self.to_dict(root=root)]


class ConfigError(MCUHomeError):
    """One problem with one place in one configuration file."""

    def __init__(
        self,
        message: str,
        *,
        location: Location | None = None,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.location = location or Location()
        self.hint = hint

    def render(self) -> str:
        lines = [f"Error: {self.message}"]
        where = self.location.describe()
        if where:
            lines.append(f"  in {where}")
        if self.hint:
            lines.append(f"  Fix: {self.hint}")
        return "\n".join(lines)

    def to_dict(self, *, root: Path | None = None) -> dict[str, Any]:
        """The three questions of this module, as fields.

        ``message`` alone, deliberately: ``str(self)`` is the rendered
        three-line form, which is the terminal's answer and not this
        one's. A caller that wants the rendering calls :meth:`render`.
        """
        return {
            "message": self.message,
            "file": self.location.relative_file(root),
            "line": self.location.line,
            "column": self.location.column,
            "key": self.location.key,
            "hint": self.hint,
            "kind": type(self).__name__,
        }

    def __str__(self) -> str:
        return self.render()


class GenerationError(ConfigError):
    """Stage 4 cannot turn this (otherwise valid) configuration into files.

    Renders exactly like a :class:`ConfigError`, because to the person
    reading it the two are the same thing: something about the
    configuration the builder cannot produce a file for. It is a separate
    type so "your configuration is wrong" (stages 1-3) and "your
    configuration is fine, but code generation has no way to express it"
    stay tellable apart in code and in tests.

    Most of what it reports is caught earlier by :mod:`mcuhome.validate`.
    It still has to exist: a model can reach stage 4 without passing
    through the validator at all — that is exactly what a remote build
    server does when a canonical model arrives over the wire
    (builder-pipeline.md §6).
    """


class BuildError(ConfigError):
    """Stage 5 cannot run, or the compiler refused what stage 4 wrote.

    Renders like a :class:`ConfigError` — same three questions, same
    shape — but usually without a location, because the problem is not in
    a configuration file: no west workspace around the builder, a missing
    build-time tool, a compiler that stopped. The ``hint`` carries the fix
    and is the whole value of the type: "the build failed" is not an
    actionable message, "gn is not on your PATH, here is where it comes
    from" is.
    """


class ConfigErrorGroup(MCUHomeError):
    """Several configuration problems, reported in one go.

    Validation deliberately does not stop at the first error: a user who
    fixes five things in one pass is happier than one who re-runs the
    builder five times. Errors are reported in file order.
    """

    def __init__(self, errors: list[ConfigError]) -> None:
        self.errors = sorted(errors, key=lambda e: e.location.sort_key())
        super().__init__(f"{len(self.errors)} configuration problem(s)")

    def render(self) -> str:
        body = "\n\n".join(error.render() for error in self.errors)
        count = len(self.errors)
        noun = "problem" if count == 1 else "problems"
        return f"{body}\n\n{count} {noun} found."

    def to_dicts(self, *, root: Path | None = None) -> list[dict[str, Any]]:
        """Every problem in the group, in the file order it reports them in.

        One pass, all markers — which is the point of collecting them
        rather than stopping at the first, and the reason an editor can
        show a whole configuration's problems at once.
        """
        return [error.to_dict(root=root) for error in self.errors]

    def to_dict(self, *, root: Path | None = None) -> dict[str, Any]:
        """The first problem of the group.

        A group is a list, so :meth:`to_dicts` is what a caller wants.
        This exists so that the single-error shape is defined for every
        error type rather than for most of them.
        """
        return self.errors[0].to_dict(root=root)

    def __str__(self) -> str:
        return self.render()


def error_dicts(exc: MCUHomeError, *, root: Path | None = None) -> list[dict[str, Any]]:
    """Flatten whatever the builder raised into a list of diagnostics.

    The group-flattening half of the serialized shape: validation
    deliberately reports every problem it found rather than the first
    (:class:`ConfigErrorGroup`), and a caller collecting diagnostics
    wants one list either way.
    """
    return exc.to_dicts(root=root)


@dataclass
class ErrorCollector:
    """Gathers errors of one pipeline stage so all of them can be shown."""

    errors: list[ConfigError] = field(default_factory=list)

    def add(
        self,
        message: str,
        *,
        location: Location | None = None,
        hint: str | None = None,
    ) -> None:
        self.errors.append(ConfigError(message, location=location, hint=hint))

    def take(self, error: ConfigError) -> None:
        self.errors.append(error)

    def __bool__(self) -> bool:
        return bool(self.errors)

    def raise_if_any(self) -> None:
        """Raise the collected errors, unless there are none."""
        if not self.errors:
            return
        if len(self.errors) == 1:
            raise self.errors[0]
        raise ConfigErrorGroup(self.errors)
