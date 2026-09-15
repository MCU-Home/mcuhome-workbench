# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""1 → 2: ``secrets/`` gets one directory per kind of secret.

Every path this module names — the old ones and the new ones — is a
**literal**, deliberately, and not the constant the package carries
today. A migration describes one moment in a project's history: what it
reads is what projects of version 1 actually have on disk, and what it
writes is what version 2 means. A constant that moves later must not
change either of them under this file.

**Nothing here overwrites a file.** A directory is moved whole where
that is possible, and entry by entry where the new directory already
exists; a name that exists in both places keeps the file in the *new*
layout, because that is the one the tools have been reading, and leaves
the old one where it is. The consequence is visible rather than silent:
the old directory stays behind holding exactly what could not move.

**It is idempotent.** Every step asks what is on disk and skips what is
already in shape, so a run that was interrupted finishes on the next
attempt and a project that is already migrated is not touched.
"""

from __future__ import annotations

import io
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from mcuhome.workbench.projectfile import ProjectFile

FROM_VERSION = 1
TO_VERSION = 2

DESCRIPTION = "Move the project's secrets into one directory per kind"

DETAILS = """
Everything under secrets/ now lives in one directory per kind of secret,
named after that kind:

    secrets/firmware/      ->  secrets/signing/
    secrets/devices/       ->  secrets/device/
    secrets/build-server/  ->  secrets/builder/

Inside the signing directory the files are named after what they hold
rather than after the bootloader that verifies them: mcuboot.yaml
becomes key.yaml, mcuboot.pem becomes key.pem and signing.pub becomes
key.pub. The reference inside key.yaml is rewritten to match, so signing
keeps working with nothing else to do.

What this means for you:

  * Your secrets keep their content and their permissions. Nothing is
    copied anywhere, nothing is deleted and nothing leaves this machine.
  * The .gitignore line for secrets/ still covers all of it.
  * If a file of the same name was already in the new place, that one is
    kept and the old one is left exactly where it was — this never
    overwrites a file of yours. The old directory then stays behind with
    whatever could not move, for you to look at and remove.
  * A script of yours that names one of the old paths has to be changed:
    MCUHome reads the new ones only.
"""


def _read_editable_yaml(path: Path) -> Any:
    """The YAML parser that leaves a ``!file`` reference a reference.

    Imported inside the call rather than at the top of the module: the
    loader reads the project layout, which reads the upgrade, which reads
    this package — an import at module level would close that circle.
    """
    from mcuhome.workbench.loader import read_editable_yaml

    return read_editable_yaml(path)


def _editing_yaml() -> Any:
    """The writer that puts a ``!file`` reference back as one.

    Imported inside the call, for the reason :func:`_read_editable_yaml`
    gives.
    """
    from mcuhome.workbench.loader import editing_yaml

    return editing_yaml()


#: The one key the signing secrets file carries: a ``!file`` reference to
#: the private key beside it.
_SIGNING_KEY_ENTRY = "firmware_signing_key"

#: Directories, old name to new: one per kind of secret, in the singular.
_DIRECTORIES = (
    ("devices", "device"),
    ("build-server", "builder"),
    ("firmware", "signing"),
)

#: Inside the signing directory, old name to new. The key file is not in
#: here: which file holds the key is what the reference says, and that is
#: read rather than guessed.
_SIGNING_FILES = (
    ("mcuboot.yaml", "key.yaml"),
    ("signing.pub", "key.pub"),
)

_SIGNING_YAML = "key.yaml"
_SIGNING_KEY_FILE = "key.pem"

#: Where an unreferenced private key is looked for, in this order, when
#: the secrets file names none. Anything else is left alone: adopting a
#: key MCUHome never wrote would be guessing which key a device out there
#: was bootstrapped with.
_ADOPTABLE_KEYS = ("key.pem", "mcuboot.pem")


def migrate(root: Path, file: ProjectFile) -> ProjectFile:
    """Move the project's secrets into the layout version 2 states."""
    secrets = Path(root) / "secrets"
    if not secrets.is_dir():
        # A project whose secrets directory was never created has
        # nothing to move; the layout is created when it is first used.
        return file
    for old, new in _DIRECTORIES:
        _move_directory(secrets / old, secrets / new)
    _migrate_signing(secrets / "signing")
    return file


# --------------------------------------------------------------------------
# Moving
# --------------------------------------------------------------------------


def _move_directory(source: Path, target: Path) -> None:
    """Move *source* to *target*, whole where it can be, entry by entry else."""
    if not source.is_dir() or source.is_symlink():
        return
    if not target.exists():
        os.replace(source, target)
        return
    if not target.is_dir():
        return
    for entry in sorted(source.iterdir()):
        _move_file(entry, target / entry.name)
    _remove_if_empty(source)


def _move_file(source: Path, target: Path) -> None:
    """Move *source* onto *target* unless something is already there."""
    if source == target or not source.exists() or target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    os.replace(source, target)


def _remove_if_empty(directory: Path) -> None:
    """Drop a directory this call emptied; leave one that still holds files."""
    try:
        directory.rmdir()
    except OSError:
        return


# --------------------------------------------------------------------------
# The signing key
# --------------------------------------------------------------------------


def _migrate_signing(directory: Path) -> None:
    """Rename the signing files and make the key reference name key.pem."""
    if not directory.is_dir():
        return
    for old, new in _SIGNING_FILES:
        _move_file(directory / old, directory / new)

    secrets_file = directory / _SIGNING_YAML
    data = _read_editable_yaml(secrets_file) if secrets_file.is_file() else None
    referenced = _referenced_name(data)
    if referenced is not None:
        if referenced == _SIGNING_KEY_FILE or "/" in referenced or "\\" in referenced:
            # Already named right, or a key the user keeps somewhere else
            # entirely — that path is theirs and stays as they wrote it.
            return
        key = directory / referenced
        if not key.is_file() or (directory / _SIGNING_KEY_FILE).exists():
            return
        os.replace(key, directory / _SIGNING_KEY_FILE)
        _rewrite_reference(secrets_file, data)
        return

    adopted = _unreferenced_key(directory)
    if adopted is None:
        return
    _move_file(adopted, directory / _SIGNING_KEY_FILE)
    _write_reference(secrets_file, data)


def _referenced_name(data: Any) -> str | None:
    """The name behind ``firmware_signing_key: !file <name>``, unfollowed.

    The file is read with the editing parser, so the reference stays a
    tag: the private key it points at is never opened here, and a
    reference to a file that is missing is a fact rather than a failure.
    """
    if not isinstance(data, dict):
        return None
    value = data.get(_SIGNING_KEY_ENTRY)
    tag = getattr(value, "tag", None)
    if getattr(tag, "value", None) != "!file":
        return None
    name = str(getattr(value, "value", "")).strip()
    return name or None


def _unreferenced_key(directory: Path) -> Path | None:
    """Key material in *directory* that no reference names, or ``None``.

    A project whose secrets file lost its entry — or never had one
    because the key was imported by hand — still has the key on disk, and
    the tools refuse to draw a second one beside it. Adopting it here is
    what makes the project usable again. Only the two names MCUHome ever
    wrote are adopted, and only when the file really holds a key:
    anything else in that directory is somebody's own material, and
    picking one of several keys would be guessing which one a device out
    there carries in its bootloader.
    """
    # Imported here, not at the top, for the reason
    # :func:`_read_editable_yaml` gives.
    from mcuhome.workbench.signing import is_p256_private_key

    for name in _ADOPTABLE_KEYS:
        candidate = directory / name
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable or binary: nothing this can judge
        if is_p256_private_key(text):
            return candidate
    return None


def _rewrite_reference(secrets_file: Path, data: Any) -> None:
    """Point the existing reference at key.pem, keeping the rest of the file."""
    data[_SIGNING_KEY_ENTRY].value = _SIGNING_KEY_FILE
    _write(secrets_file, _render(data))


def _write_reference(secrets_file: Path, data: Any) -> None:
    """Reference the adopted key, in the file if there is one, else a new one."""
    if data is None:
        _write(
            secrets_file,
            "# MCUHome firmware signing key.\n"
            f"# The private half of the project's key pair lives next to this\n"
            f"# file as {_SIGNING_KEY_FILE} and is referenced below. It never\n"
            "# leaves this machine: never commit it, never copy it into a build\n"
            "# directory, never hand it to a build server.\n"
            f"{_SIGNING_KEY_ENTRY}: !file {_SIGNING_KEY_FILE}\n",
        )
        return
    text = _render(data)
    if text and not text.endswith("\n"):
        text += "\n"
    _write(secrets_file, f"{text}{_SIGNING_KEY_ENTRY}: !file {_SIGNING_KEY_FILE}\n")


def _render(data: Any) -> str:
    """The parsed document back as text, comments and quoting included."""
    stream = io.StringIO()
    _editing_yaml().dump(data, stream)
    return stream.getvalue()


def _write(target: Path, text: str) -> None:
    """Replace *target* with *text* in one step, or leave it as it was.

    A secrets file is written the way every other writer in this package
    writes one: into a temporary file beside it, created owner-only, and
    moved over the target. A migration that is interrupted mid-write must
    not be able to leave half a reference behind — the next run has to
    find either the old file or the new one.
    """
    mode = stat.S_IMODE(target.stat().st_mode) if target.is_file() else 0o600
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
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
        Path(temporary).unlink(missing_ok=True)
        raise
