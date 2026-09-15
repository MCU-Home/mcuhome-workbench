# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Looking at, and changing, a project's secrets.

A project keeps everything that must not be committed under
``secrets/``: the values a device configuration reaches with ``!secret``
(a WiFi password, a device's commissioning credentials), the credentials
a build server wants, and the firmware signing key. Until now this
package only ever *read* those files on the way to something else — a
build, a validation, a signature — and the one thing a person or a
dashboard actually needs, "which secrets does this project have and what
is in them", was not on the surface at all.

These six calls are that surface, and they are the only supported way to
look at those files.

**No answer of theirs ever carries a value.** :func:`read_secrets`
answers the keys with :data:`MASKED_VALUE` in place of every value, and
the mask is a constant: it is not derived from the value, so neither its
length nor its first character nor whether two keys hold the same thing
leaks into a document that a client may log, cache or send somewhere.
The one call that answers a value is :func:`reveal_secret`, and it has a
verb of its own for that reason — a caller asks for exactly one key, by
name, and cannot get a value by accident out of a call that was meant to
list something.

**An exposed file is refused, not read.** Every one of the six runs
:func:`~mcuhome.workbench.project.require_secret_file` over the file
first and treats it as key material: a file group or world can reach is
a refusal naming the ``chmod`` that fixes it. That is deliberately
stricter than the build path, which only *warns* about a readable
``secrets/main.yaml`` — refusing to compile firmware over the mode of a
file would help nobody, while a call whose entire subject is those
values must not hand them out of a file that is already handing them to
everyone else.

**Nothing here follows a ``!file`` reference** — not in a secrets file
and not in a device configuration. Every file these calls read is parsed
with :func:`~mcuhome.workbench.loader.read_editable_yaml`, so a tagged
entry stays a tag: listing the signing scope reads the *name* of the
entry that points at the key and never a byte of the key itself
(:func:`reveal_secret` refuses such an entry outright), and the
``used_by`` scan reads the ``!secret`` names out of the devices' own
configurations without opening whatever else those point at. The same
parse is what makes a write a round trip — comments, order, blank lines
and every other entry survive :func:`set_secret` and :func:`unset_secret`
exactly as they were.

**What the four kinds are.** :data:`SECRET_KINDS` is the vocabulary, and
each kind says where its file is and who names it:

``main``
    ``secrets/main.yaml``, the project's shared values. One file, no
    name.
``device``
    one file per device, written by ``mcuhome device matter-pairing``
    and read by ``!secret`` before the shared file.
``builder``
    one file per named builder, holding the ``token`` a build server
    wants.
``signing``
    the project's firmware signing key: a YAML entry referencing the key
    file beside it. Key material is never set through a text field and
    never printed, so this scope is readable and
    :func:`set_secret` refuses it — the key is drawn by
    :func:`~mcuhome.workbench.signing.create_signing_key`.

:func:`delete_secret_file` removes a whole ``device`` or ``builder``
file and refuses for ``main`` and ``signing``: those two are the
project's own, they are emptied key by key with :func:`unset_secret`,
and removing one under a user's feet would take the project's shared
values or its identity as a firmware vendor with it.

``docs/api.md`` in this repository states the contract these functions
are held to, and ``tests/python/test_secrets.py`` holds them to it.
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from mcuhome.model.errors import ConfigError, Location

from mcuhome.workbench.loader import (
    editing_yaml,
    read_editable_yaml,
    secret_references,
)
from mcuhome.workbench.project import Project, ensure_secrets_dir, require_secret_file

__all__ = [
    "MASKED_VALUE",
    "SECRET_KINDS",
    "SecretFile",
    "SecretKey",
    "SecretScope",
    "delete_secret_file",
    "find_secret_scopes",
    "read_secrets",
    "reveal_secret",
    "set_secret",
    "unset_secret",
]

#: The kinds of secret a project keeps, in the order this package lists
#: them: the shared file, then one file per device, one per named
#: builder, and the firmware signing key.
SECRET_KINDS = ("main", "device", "builder", "signing")

#: The kinds whose file belongs to the project as a whole rather than to
#: something a user named. They carry no name and are never deleted as a
#: file — :func:`unset_secret` empties them key by key.
_PROJECT_OWN_KINDS = ("main", "signing")

#: What :func:`read_secrets` answers instead of a value. A constant, not
#: a redaction of the value: a mask that kept the length, the first
#: character or the shape of what it hides would put part of the secret
#: into every document that shows it.
MASKED_VALUE = "********"

#: What a secrets file this package creates is created with. The
#: directories on the way to it are 0700
#: (:func:`~mcuhome.workbench.project.ensure_secrets_dir`); an existing
#: file keeps the mode its owner gave it.
_NEW_FILE_MODE = 0o600

#: The header a secrets file this package creates starts with. A user
#: who opens the file afterwards has to be able to tell what it is and
#: why it must not be committed.
_NEW_FILE_HEADER = (
    "# MCUHome secrets. `!secret <name>` in a device configuration reads\n"
    "# from here. Keep this file out of version control.\n"
)


@dataclass(frozen=True)
class SecretScope:
    """One secrets file of a project — the kind, the name, the path.

    A scope is a *place* a secret can be, whether or not anything is
    there yet: :attr:`exists` is what says which of the two it is, so a
    client can offer "add a secret for this device" for a device that
    has no file.
    """

    #: One of :data:`SECRET_KINDS`.
    kind: str
    #: The device or builder this file belongs to; empty for the two
    #: kinds that belong to the project itself.
    name: str
    #: Where the file is, whether or not it is there.
    file: Path
    #: Whether the file exists right now.
    exists: bool

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready, and carrying no value — a scope is a location."""
        return {
            "kind": self.kind,
            "name": self.name,
            "file": str(self.file),
            "exists": self.exists,
        }


@dataclass(frozen=True)
class SecretKey:
    """One entry of a secrets file: its name, a mask, and who reads it."""

    #: The name a ``!secret`` reference uses.
    key: str
    #: :data:`MASKED_VALUE`. The value is answered by
    #: :func:`reveal_secret` and by nothing else.
    masked: str
    #: The devices whose ``!secret`` references reach *this* entry, by
    #: device name. Empty for a kind no device reads, and for an entry a
    #: device's own file shadows.
    used_by: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "masked": self.masked, "used_by": list(self.used_by)}


@dataclass(frozen=True)
class SecretFile:
    """What one secrets file holds: its scope and its keys, never a value."""

    scope: SecretScope
    #: In the order the file spells them, so a client shows the file.
    keys: tuple[SecretKey, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope.to_dict(), "keys": [key.to_dict() for key in self.keys]}


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------


def _refuse_unknown_kind(kind: str) -> ConfigError:
    return ConfigError(
        f'There is no kind of secret called "{kind}".',
        hint="the kinds a project keeps are: " + ", ".join(SECRET_KINDS),
    )


def _refuse_named(kind: str) -> ConfigError:
    return ConfigError(
        f"The {kind} secrets belong to the project as a whole and have no name.",
        hint=f"ask for the {kind} secrets without a name",
    )


def _refuse_unnamed(kind: str) -> ConfigError:
    return ConfigError(
        f"A {kind} secrets file belongs to one {kind}, and none was named.",
        hint=f"say which {kind} the secrets belong to",
    )


def _refuse_bad_name(kind: str, name: str) -> ConfigError:
    return ConfigError(
        f"{name!r} is not a usable {kind} name.",
        hint=(
            "use lowercase letters, digits and dashes, starting with a letter or "
            "digit — the name becomes the name of a file in the project's secrets "
            "directory, and only one word can be one"
        ),
    )


def _refuse_unknown_device(project: Project, name: str, file: Path) -> ConfigError:
    known = ", ".join(project.device_names()) or "none yet"
    return ConfigError(
        f'There is no device called "{name}" in this project, and no {file}.',
        location=Location(file=project.root),
        hint=f"devices in {project.root}: {known}",
    )


def _refuse_no_file(scope: SecretScope) -> ConfigError:
    return ConfigError(
        f"{scope.file} does not exist.",
        location=Location(file=scope.file),
        hint="there are no secrets to read here yet",
    )


def _refuse_not_a_mapping(file: Path) -> ConfigError:
    return ConfigError(
        f"{file.name} must be a list of `name: value` pairs.",
        location=Location(file=file, line=1, column=1),
        hint="write one secret per line, for example:\n    wifi_password: your-value-here",
    )


def _refuse_unknown_key(scope: SecretScope, key: str, known: tuple[str, ...]) -> ConfigError:
    listing = ", ".join(known) or "none"
    return ConfigError(
        f'There is no secret called "{key}" in {scope.file}.',
        location=Location(file=scope.file, key=key),
        hint=f"secrets in this file: {listing}",
    )


def _refuse_empty_key() -> ConfigError:
    return ConfigError(
        "A secret needs a name.",
        hint="name the secret a device configuration reads, for example wifi_password",
    )


def _refuse_not_plain(scope: SecretScope, key: str, value: Any) -> ConfigError:
    """An entry that is not one value: a file reference, or a structure."""
    if hasattr(value, "tag"):
        return ConfigError(
            f'The entry "{key}" in {scope.file} points at a file, and MCUHome does '
            "not print what is in it.",
            location=Location(file=scope.file, key=key),
            hint=(
                "an entry that references a file holds key material — the firmware "
                "signing key is the one this project has, and its private half never "
                "leaves this machine"
            ),
        )
    return ConfigError(
        f'The entry "{key}" in {scope.file} is not a single value.',
        location=Location(file=scope.file, key=key),
        hint="a secret is one `name: value` line; this entry holds a list or a section",
    )


def _refuse_signing_write(scope: SecretScope, key: str) -> ConfigError:
    return ConfigError(
        f"MCUHome does not set {key} in {scope.file}.",
        location=Location(file=scope.file, key=key),
        hint=(
            "the firmware signing key is a file, not a value typed into one: MCUHome "
            "draws it the first time it signs an image for this project, and an entry "
            "written here by hand would be refused by the next signature. To sign with "
            "a key you already have, point --signing-key at its PEM file or set the "
            "option signing.key"
        ),
    )


def _refuse_delete(file: Path) -> ConfigError:
    return ConfigError(
        f"{file} belongs to the project itself and is not deleted as a file.",
        location=Location(file=file),
        hint=(
            "the shared secrets and the firmware signing key are what the project's "
            "devices are built and signed with, so they are emptied one entry at a "
            "time rather than removed in one go"
        ),
    )


def _refuse_unwritable(file: Path, reason: str) -> ConfigError:
    return ConfigError(
        f"MCUHome cannot write {file}: {reason}.",
        location=Location(file=file),
        hint="check that the project's secrets directory is yours and writable",
    )


# --------------------------------------------------------------------------
# Scopes
# --------------------------------------------------------------------------


def _scope_file(project: Project, kind: str, name: str) -> Path:
    """Where one scope's file is — the layout is the project's to state."""
    if kind == "main":
        return project.secrets_file
    if kind == "signing":
        return project.signing_secrets_file
    if kind == "device":
        return project.device_secrets_file(name)
    return project.builder_secrets_file(name)


#: What a device or a builder may be called, and therefore what a scope
#: may be called: the rule
#: :func:`~mcuhome.workbench.builders.builders_of` already holds a
#: builder name to, and a superset of the device-name rule
#: :func:`~mcuhome.workbench.scaffold.create_device` holds a device to
#: (which demands a letter on top of it, because a device name becomes a
#: hostname). One word of lowercase letters, digits and dashes — a name
#: becomes a file name in the project's secrets directory, and anything
#: that is not one word is a path, a shell surprise, or a byte an
#: operating system refuses halfway through a write.
_SCOPE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _is_scope_name(name: str) -> bool:
    """Whether *name* is a name and not something spelled like one."""
    return bool(_SCOPE_NAME_RE.match(name))


def _scope(project: Project, kind: str, name: str) -> SecretScope:
    """The scope *kind* and *name* denote, or a refusal that says why not.

    The name rules are the whole validation: the two project-own kinds
    take none, the two named kinds need one, and a name is one plain word
    because it becomes a file name in the project's secrets directory. A
    ``device`` name is checked against the project on top of that — a
    project knows its devices, so a typo is caught here instead of
    creating a secrets file for a device that does not exist. A
    ``builder`` is configured in a configuration file this call does not
    read, so any plain name is a scope; whether anything is there is
    :attr:`SecretScope.exists`.
    """
    if kind not in SECRET_KINDS:
        raise _refuse_unknown_kind(kind)
    if kind in _PROJECT_OWN_KINDS:
        if name:
            raise _refuse_named(kind)
    elif not name:
        raise _refuse_unnamed(kind)
    elif not _is_scope_name(name):
        raise _refuse_bad_name(kind, name)
    file = _scope_file(project, kind, name)
    exists = file.is_file()
    if kind == "device" and not exists and name not in project.device_names():
        raise _refuse_unknown_device(project, name, file)
    return SecretScope(kind=kind, name=name, file=file, exists=exists)


def _guard(scope: SecretScope) -> None:
    """Refuse an exposed file before anything reads or writes it."""
    if scope.exists:
        require_secret_file(scope.file, key_material=True)


def _names_in(directory: Path) -> list[str]:
    """The names the ``<name>.yaml`` files in *directory* carry.

    A file whose stem is not a name this package could have written is
    not a scope and is left out: every call here takes a name, so a scope
    nothing can address would be listed and then refused. What such a
    file is remains visible where it lies.
    """
    if not directory.is_dir():
        return []
    return sorted(
        entry.stem
        for entry in directory.glob("*.yaml")
        if entry.is_file() and _is_scope_name(entry.stem)
    )


def find_secret_scopes(project: Project) -> tuple[SecretScope, ...]:
    """Every secrets file *project* has or could have.

    The two the project always has — the shared file and the signing key
    — plus one per device and one per builder credentials file that is
    actually there. A device of the project is listed whether or not it
    has a file yet, so a client can offer to create one; a file left
    behind by a device that was deleted is listed too, so it can be found
    and removed instead of lying around unnoticed.

    Builders come from the files alone: which builders a project has is a
    question for the configuration ladder, which this call does not read.

    Never answers an empty tuple: a project without a ``secrets/``
    directory at all still has those two scopes, both with ``exists``
    false.
    Raises :class:`~mcuhome.model.errors.ConfigError` for a secrets file
    other users can reach: this is the entry point of the whole surface,
    and a mode that has to be fixed is better said here than four calls
    later.
    """
    devices = sorted(set(project.device_names()) | set(_names_in(project.device_secrets_dir)))
    scopes = [
        _scope(project, "main", ""),
        *(_scope(project, "device", name) for name in devices),
        *(_scope(project, "builder", name) for name in _names_in(project.builder_secrets_dir)),
        _scope(project, "signing", ""),
    ]
    for scope in scopes:
        _guard(scope)
    return tuple(scopes)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _is_plain(value: Any) -> bool:
    """Whether an entry holds one value rather than a reference or a structure."""
    return isinstance(value, str | int | float | bool | type(None))


def _entries(scope: SecretScope) -> dict[str, Any]:
    """The file's entries in the order it spells them, tags left as tags."""
    if not scope.exists:
        return {}
    data = read_editable_yaml(scope.file)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _refuse_not_a_mapping(scope.file)
    return dict(data)


def _device_references(project: Project, device: str) -> tuple[str, ...]:
    """Which secrets *device*'s configuration names, without resolving one.

    The editing parse again, and here it matters twice over: a device
    file may hold ``!file`` references of its own, and the question
    "which devices read this secret" must not answer itself by reading a
    certificate, a key, or whatever else somebody pointed a configuration
    at. So the tags stay tags — the ``!secret`` names are read off them —
    and a device whose ``!file`` neighbour is missing, unreadable or
    exposed is answered from its references like any other.

    A device whose file is missing or is not valid YAML names nothing as
    far as this is concerned: what is wrong with it is
    :func:`~mcuhome.workbench.validate.validate`'s to say, and a list of
    who uses a secret must not be the place a broken configuration is
    reported.
    """
    entry = project.device_file(device)
    if not entry.is_file():
        return ()
    try:
        return secret_references(read_editable_yaml(entry))
    except ConfigError:
        return ()


def _used_by(project: Project, scope: SecretScope, keys: tuple[str, ...]) -> dict[str, list[str]]:
    """Which devices reach each of *keys* in *scope*, following the ladder.

    ``!secret`` reads a device's own file first and the shared file
    second, so a device that defines a name itself does **not** use the
    shared entry of that name — and this says so, rather than listing
    every device that mentions the word.
    """
    users: dict[str, list[str]] = {key: [] for key in keys}
    if scope.kind == "device":
        for key in _device_references(project, scope.name):
            if key in users:
                users[key].append(scope.name)
        return users
    if scope.kind != "main":
        return users
    for device in project.device_names():
        own_scope = _scope(project, "device", device)
        _guard(own_scope)
        own = _entries(own_scope)
        for key in _device_references(project, device):
            if key in users and key not in own:
                users[key].append(device)
    return users


def read_secrets(project: Project, *, kind: str, name: str = "") -> SecretFile:
    """The keys of one secrets file, masked, and who reads them.

    Every value is :data:`MASKED_VALUE` — this call is how a client shows
    a file, and no document it answers carries anything of what is in it.
    For the shared file, each key also carries the devices whose
    ``!secret`` references reach it (:attr:`SecretKey.used_by`), which is
    what makes "is this still used" answerable before a value is removed.

    A scope whose file does not exist answers with no keys and
    ``scope.exists`` false, rather than refusing: a client that lists the
    scopes has to be able to open one that is still empty. What is
    refused is a scope that is not one at all — an unknown kind, a name
    where none belongs or none where one does, a device the project does
    not have — and a file other users can reach.
    """
    scope = _scope(project, kind, name)
    _guard(scope)
    entries = _entries(scope)
    keys = tuple(str(key) for key in entries)
    users = _used_by(project, scope, keys)
    return SecretFile(
        scope=scope,
        keys=tuple(
            SecretKey(key=key, masked=MASKED_VALUE, used_by=tuple(users.get(key, ())))
            for key in keys
        ),
    )


def _as_text(value: Any) -> str:
    """One value as the text a person sees, in YAML's own spelling."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def reveal_secret(project: Project, *, kind: str, name: str = "", key: str) -> str:
    """The one value *key* holds — the only call here that answers one.

    A verb of its own, so that the security-relevant call cannot be made
    by accident: a caller asks for exactly this key of exactly this
    scope, and gets nothing else. Everything else about the surface
    masks.

    Refuses in words for a key the file does not hold, for a file that is
    not there, for a file other users can reach — and for an entry that
    references a file, which is how key material is kept: the firmware
    signing key is a file whose private half nothing prints. A value the
    file spells as a number or a boolean is answered as it reads in the
    file; an entry with no value at all answers with the empty string.
    """
    scope = _scope(project, kind, name)
    _guard(scope)
    if not scope.exists:
        raise _refuse_no_file(scope)
    entries = _entries(scope)
    if key not in entries:
        raise _refuse_unknown_key(scope, key, tuple(str(entry) for entry in entries))
    value = entries[key]
    if not _is_plain(value):
        raise _refuse_not_plain(scope, key, value)
    return _as_text(value)


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------


def _write(project: Project, scope: SecretScope, data: Any, *, prefix: str = "") -> None:
    """Put *prefix* and *data* where the scope's file is — all of it or none.

    The whole file is written every time, so the old one has to survive
    until the new one is complete: a truncate-and-write that is
    interrupted — a full disk, a process killed, a machine losing power —
    leaves half a secrets file, and half a secrets file is a project that
    cannot build and credentials nobody can read out of it any more. So
    the text goes into a temporary file in the same directory, created
    owner-only, flushed to the disk, and then moved over the target in
    one step: a reader sees either the file that was there or the file
    that replaced it.

    A file that is already there keeps the mode its owner gave it, and a
    symlink somebody put in the layout is followed rather than replaced —
    the file they pointed at is the file that gets written. A file this
    call brings into existence is created with mode 0600, and the
    directories on the way to it with 0700.
    """
    rendered = prefix + _render(editing_yaml(), data)
    target = scope.file
    mode = _NEW_FILE_MODE
    if target.is_file():
        target = target.resolve()
        mode = stat.S_IMODE(target.stat().st_mode)
    else:
        parts = scope.file.parent.relative_to(project.secrets_dir).parts
        ensure_secrets_dir(project.root, *parts)
    try:
        _replace_atomically(target, rendered, mode)
    except OSError as error:
        raise _refuse_unwritable(scope.file, error.strerror or "cannot write") from error


def _replace_atomically(target: Path, text: str, mode: int) -> None:
    """Write *text* beside *target* and move it over, or leave *target* alone.

    ``mkstemp`` creates the temporary file owner-only and exclusively, so
    the content is never readable by anyone else even for the moment it
    lies beside the real file; it is hidden and carries this package's
    prefix, so a crash between the two steps leaves something a person
    can recognize. The directory is flushed after the move, because a
    rename that is not on the disk is a rename that did not happen.
    """
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent, prefix=".mcuhome-secrets-", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    _flush_directory(target.parent)


def _flush_directory(directory: Path) -> None:
    """Make the move itself durable, where the platform allows saying so."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:  # pragma: no cover - a platform that will not open one
        return
    try:
        os.fsync(descriptor)
    except OSError:  # pragma: no cover - and one that will not flush one
        pass
    finally:
        os.close(descriptor)


def _ending_in_newline(file: Path) -> str:
    """The file's text, with the newline a next line would need."""
    text = file.read_text(encoding="utf-8")
    return text if not text or text.endswith("\n") else text + "\n"


def _render(yaml: Any, data: Any) -> str:
    """The document as text — and an emptied file is empty, not ``{}``.

    A round-trip dump of a mapping with nothing left in it writes the
    flow-style ``{}``, which is correct YAML and wrong here: what the
    user sees after removing the last secret from a file is a file that
    says nothing, with their own comments still in it, rather than a
    token they did not write and would have to look up.
    """
    buffer = StringIO()
    yaml.dump(data, buffer)
    text = buffer.getvalue()
    if not data:
        text = "".join(line for line in text.splitlines(keepends=True) if line.strip() != "{}")
    return text


def set_secret(project: Project, *, kind: str, name: str = "", key: str, value: str) -> None:
    """Set one secret, leaving the rest of the file exactly as it was.

    The file is read and written through the round-trip parser, so
    comments, order, blank lines, quoting style and every other entry
    survive the edit; a new key is appended. The first secret of a scope
    creates the file with mode 0600 and the directories to it with 0700.

    Refuses the ``signing`` scope: the firmware signing key is a file,
    drawn by
    :func:`~mcuhome.workbench.signing.create_signing_key`, and a value
    typed in here would be refused by the next signature. Refuses an
    entry that currently references a file, for the same reason — a
    reference replaced by a value would silently unhook key material that
    is still on disk.
    """
    scope = _scope(project, kind, name)
    if not key:
        raise _refuse_empty_key()
    if scope.kind == "signing":
        raise _refuse_signing_write(scope, key)
    _guard(scope)
    data = read_editable_yaml(scope.file) if scope.exists else None
    if data is None:
        # No mapping to add to: a file that is not there yet gets the
        # header that says what it is, and one that holds only comments
        # — what a file emptied by `unset_secret` looks like — keeps them
        # and gets the entry appended after them.
        prefix = _NEW_FILE_HEADER if not scope.exists else _ending_in_newline(scope.file)
        _write(project, scope, {key: value}, prefix=prefix)
        return
    if not isinstance(data, dict):
        raise _refuse_not_a_mapping(scope.file)
    if key in data and not _is_plain(data[key]):
        raise _refuse_not_plain(scope, key, data[key])
    data[key] = value
    _write(project, scope, data)


def unset_secret(project: Project, *, kind: str, name: str = "", key: str) -> bool:
    """Remove one secret; ``False`` when the file did not hold it.

    The rest of the file is untouched, and removing the **last** entry
    leaves an empty file rather than a deleted one or a ``{}``: the file
    is the user's, and this call was asked to remove one secret.
    """
    scope = _scope(project, kind, name)
    _guard(scope)
    if not scope.exists:
        return False
    data = read_editable_yaml(scope.file)
    if data is None:
        return False
    if not isinstance(data, dict):
        raise _refuse_not_a_mapping(scope.file)
    if key not in data:
        return False
    del data[key]
    _write(project, scope, data)
    return True


def delete_secret_file(project: Project, *, kind: str, name: str) -> bool:
    """Remove a whole ``device`` or ``builder`` secrets file.

    Answers whether there was one. Refuses for ``main`` and ``signing``:
    those two belong to the project itself — the values every device
    shares, and the key the project's firmware is signed with — and are
    emptied entry by entry with :func:`unset_secret` instead of
    disappearing in one call.

    A file other users can reach is refused here as everywhere else: the
    mode is fixed first, and then it is deleted.
    """
    if kind not in SECRET_KINDS:
        raise _refuse_unknown_kind(kind)
    if kind in _PROJECT_OWN_KINDS:
        raise _refuse_delete(_scope_file(project, kind, ""))
    scope = _scope(project, kind, name)
    _guard(scope)
    if not scope.exists:
        return False
    try:
        scope.file.unlink()
    except OSError as error:
        raise _refuse_unwritable(scope.file, error.strerror or "cannot remove") from error
    return True
