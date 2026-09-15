# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Findings that are not failures: located, typed, renderable.

An error stops the work and carries everything a person needs to fix it
— a message, the file and line it is about, a hint. A **warning** is the
same kind of statement about something that did not stop the work, and
until now it left this package as a bare line of text: a client could
print it and nothing else. It could not show it where the problem is,
could not group two of them, and could not tell one kind from another.

:class:`Diagnostic` is that missing half. It carries the error
document's own fields plus a :attr:`~Diagnostic.severity`, so one
``diagnostics`` list holds the errors and the warnings of a run together
and a client renders it with one piece of code. Its
:attr:`~Diagnostic.kind` is what a client switches on: for an error that
is the exception's class name, for a warning one of
:data:`WARNING_KINDS`.

The vocabulary is a fixed set rather than free text because it is what a
client recognizes across releases: a message is written for a person and
may be reworded at any time, a kind is written for a program and is
therefore append-only. Nothing in this module reads a file, formats for
a terminal or decides what to do about a finding — it states one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcuhome.model.errors import Location

__all__ = [
    "SEVERITIES",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "WARNING_KINDS",
    "Diagnostic",
]

#: A finding about something that stopped the work.
SEVERITY_ERROR = "error"

#: A finding about something that did not.
SEVERITY_WARNING = "warning"

#: What a finding's :attr:`Diagnostic.severity` may be, most serious
#: first. Two values and a published tuple all the same: a client that
#: sorts or filters a `diagnostics` list reads them off this rather than
#: off the string literals in its own source.
SEVERITIES: tuple[str, ...] = (SEVERITY_ERROR, SEVERITY_WARNING)

#: Every kind of warning this package reports, as the value that travels
#: in :attr:`Diagnostic.kind`. Lowercase with underscores, append-only:
#: a client that recognizes one of these renders it its own way — a
#: permission warning next to the file it is about, an unverified
#: registry as the loud banner it deserves — and a value it does not
#: know is still shown by its message. An error's ``kind`` is the
#: exception's class name instead, because there the class *is* the
#: vocabulary.
WARNING_KINDS: tuple[str, ...] = (
    # A secrets file group or world can read. The read went ahead: only
    # key material is refused outright.
    "exposed_secret_file",
    # A package registry is being read without checking any signature,
    # because the project configured it as untrusted.
    "unverified_registry",
    # An environment variable MCUHome used to read is set. It is not
    # read any more, and the successor is named in the message.
    "retired_environment_variable",
)


@dataclass(frozen=True)
class Diagnostic:
    """One finding, in the shape of the error document plus its severity.

    The fields are the error document's, so that a client renders a
    warning with the code it already has for an error:
    :attr:`message` is written for the person who hit it, :attr:`location`
    says where, :attr:`hint` names the fix, and :attr:`kind` says which
    finding this is.

    :attr:`location` is the model's own :class:`~mcuhome.model.errors.Location`
    rather than three loose fields, because it is the same value a
    :class:`~mcuhome.model.errors.ConfigError` carries and the two are
    compared, sorted and rendered together. ``key`` is read off it
    (:attr:`key`) instead of being stored twice — one fact in two places
    is one fact too many.

    Frozen: a finding is a statement about something that already
    happened, and a caller that received one through ``on_warning``
    holds the same object the result carries.
    """

    #: One of :data:`SEVERITIES`.
    severity: str
    message: str
    #: One of :data:`WARNING_KINDS` for a warning, the exception's class
    #: name for an error.
    kind: str
    #: Where the finding is about. The empty location — no file, no line
    #: — is a finding about nothing in particular, which renders without
    #: a place rather than with a wrong one.
    location: Location = field(default_factory=Location)
    #: What to do about it, in the words a user can act on.
    hint: str | None = None

    @property
    def key(self) -> str | None:
        """The dotted configuration key this is about, where there is one."""
        return self.location.key

    @classmethod
    def warning(
        cls,
        message: str,
        *,
        kind: str,
        location: Location | None = None,
        hint: str | None = None,
    ) -> Diagnostic:
        """A warning of *kind*, which must be one of :data:`WARNING_KINDS`.

        The check is here rather than in every caller, and it is a
        ``ValueError`` rather than a user-facing refusal: a kind outside
        the published set is a mistake in this package, made where the
        warning is written, and it must not reach a client that switches
        on the value.
        """
        if kind not in WARNING_KINDS:
            raise ValueError(f"{kind!r} is not one of WARNING_KINDS: {', '.join(WARNING_KINDS)}")
        return cls(
            severity=SEVERITY_WARNING,
            message=message,
            kind=kind,
            location=location or Location(),
            hint=hint,
        )

    def to_dict(self, *, root: Path | None = None) -> dict[str, Any]:
        """This finding as the one document both severities share.

        The error document's keys with :attr:`severity` in front of them.
        *root* is the directory file paths are named relative to — the
        project, usually — exactly as
        :meth:`mcuhome.model.errors.MCUHomeError.to_dict` takes it, so a
        result can render its errors and its warnings with the same root
        and get one consistent list.
        """
        return {
            "severity": self.severity,
            "message": self.message,
            "file": self.location.relative_file(root),
            "line": self.location.line,
            "column": self.location.column,
            "key": self.location.key,
            "hint": self.hint,
            "kind": self.kind,
        }
