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
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ConfigError",
    "ConfigErrorGroup",
    "ErrorCollector",
    "Location",
    "MCUHomeError",
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


class MCUHomeError(Exception):
    """Base class for every error the CLI is allowed to show to a user."""

    def render(self) -> str:  # pragma: no cover - overridden everywhere
        return str(self)


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

    def __str__(self) -> str:
        return self.render()


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

    def __str__(self) -> str:
        return self.render()


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
