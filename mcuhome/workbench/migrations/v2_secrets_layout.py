# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""1 → 2: ``secrets/`` gets one directory per kind of secret.

Every path this module names — the old ones and the new ones — is a
**literal**, deliberately, and not the constant the package carries
today. A migration describes one moment in a project's history: what it
reads is what projects of version 1 actually have on disk, and what it
writes is what version 2 means. A constant that moves later must not
change either of them under this file.

**Everything is decided before anything moves.** A preflight walks the
whole ``secrets/`` tree first and refuses — naming the file and the one
thing to do about it — for every shape that cannot be migrated without
guessing: two signing directories, two files for the same thing, a
reference to a key that is not there, key material under a name nothing
points at, a link where a directory belongs. A project that passes the
preflight is then moved, and the move itself never has to decide
anything. The rule the preflight exists for: **no key material is ever
overwritten and the project's signing identity is never changed
silently** — a device only accepts images signed with the key its
bootloader carries, so a project holding two candidate keys is a
question for its owner, not for this module.

**The moves are repeatable.** Each of them asks what is on disk and
skips what is already in shape, so a run that was interrupted anywhere
finishes on the next attempt and a project that is already migrated is
not touched. The one half-done state a crash can leave — the key file
renamed, its reference not yet rewritten — is completed rather than
reported.

**What is not decided here**: a device's or a builder's file that exists
under both names. Those hold no identity, so the one already in the new
layout is kept, the old one stays untouched in the old directory, and
that directory stays behind with it.
"""

from __future__ import annotations

import io
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from mcuhome.model.errors import Location

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

  * Your secrets keep their content and their permissions. No file is
    copied, no file is deleted and nothing leaves this machine. An old
    directory the move leaves empty is removed with it; one that still
    holds something stays where it is.
  * The .gitignore line for secrets/ still covers all of it.
  * Nothing here overwrites a file of yours. Anything this upgrade
    cannot move without guessing — two signing keys, a reference to a
    key that is not there, key material under a name MCUHome does not
    use — is refused before the first file is touched, naming the file
    and what to do with it.
  * A device or builder file that already exists under both names is the
    one exception, because neither holds the project's identity: the
    file in the new directory is kept, the old one is left exactly where
    it is, and the old directory (secrets/devices/, secrets/build-server/)
    stays behind holding it, for you to look at and remove.
  * A script of yours that names one of the old paths has to be changed:
    MCUHome reads the new ones only.
"""

#: The one key the signing secrets file carries: a ``!file`` reference to
#: the private key beside it.
_SIGNING_KEY_ENTRY = "firmware_signing_key"

#: Directories, old name to new: one per kind of secret, in the singular.
_DIRECTORIES = (
    ("devices", "device"),
    ("build-server", "builder"),
    ("firmware", "signing"),
)

_OLD_SIGNING_DIR = "firmware"
_SIGNING_DIR = "signing"

#: Inside the signing directory, old name to new. The key file is in here
#: for the preflight — which refuses both spellings at once — while the
#: move itself goes by what the reference says rather than by the name.
_SIGNING_FILES = (
    ("mcuboot.yaml", "key.yaml"),
    ("mcuboot.pem", "key.pem"),
    ("signing.pub", "key.pub"),
)

#: What the move renames by name alone: neither carries the key.
_RENAMED_BY_NAME = (("mcuboot.yaml", "key.yaml"), ("signing.pub", "key.pub"))

_SIGNING_YAML = "key.yaml"
_OLD_SIGNING_YAML = "mcuboot.yaml"
_SIGNING_KEY_FILE = "key.pem"

#: The names MCUHome itself ever wrote a private key under. Key material
#: in the signing directory under any other name is not adopted — it is
#: refused, because picking one of two keys would be guessing which one a
#: device out there carries in its bootloader.
_ADOPTABLE_KEYS = ("key.pem", "mcuboot.pem")


def migrate(root: Path, file: ProjectFile) -> ProjectFile:
    """Move the project's secrets into the layout version 2 states."""
    secrets = Path(root) / "secrets"
    if not secrets.is_dir():
        # A project whose secrets directory was never created has
        # nothing to move; the layout is created when it is first used.
        return file
    _preflight(secrets)
    for old, new in _DIRECTORIES:
        _move_directory(secrets / old, secrets / new)
    _migrate_signing(secrets / _SIGNING_DIR)
    return file


# --------------------------------------------------------------------------
# The preflight: everything this cannot do without guessing, before it starts
# --------------------------------------------------------------------------


def _preflight(secrets: Path) -> None:
    """Refuse every shape the move would have to guess about. Moves nothing."""
    for old, new in _DIRECTORIES:
        _check_directories(secrets / old, secrets / new)
    _check_signing(secrets)


def _check_directories(source: Path, target: Path) -> None:
    """The two directories of one kind, before anything is moved between them.

    The target is examined whether or not there is anything to move into
    it. A link or a plain file where a secrets directory belongs is the
    same problem either way: the project writes its secrets there from
    now on, and a link would put them somewhere else on the disk under
    permissions this project does not set. An empty source is not a
    reason to leave that unsaid.
    """
    if target.is_symlink():
        raise _refuse_link(target)
    if target.exists() and not target.is_dir():
        raise _refuse_not_a_directory(target)
    if not source.exists() and not source.is_symlink():
        return  # nothing to move out of; the target has been looked at
    if source.is_symlink():
        raise _refuse_link(source)
    if not source.is_dir():
        raise _refuse_not_a_directory(source)


def _check_signing(secrets: Path) -> None:
    """The signing directory: one directory, one file per thing, one key."""
    old_dir = secrets / _OLD_SIGNING_DIR
    new_dir = secrets / _SIGNING_DIR
    if old_dir.is_dir() and new_dir.exists():
        raise _refuse_two_signing_directories(old_dir, new_dir)
    directory = old_dir if old_dir.is_dir() else new_dir
    if not directory.is_dir():
        return

    for old, new in _SIGNING_FILES:
        if (directory / old).exists() and (directory / new).exists():
            raise _refuse_two_files(directory / old, directory / new)

    secrets_file = _signing_secrets_file(directory)
    if secrets_file is None:
        _check_unreferenced_key(directory, referenced=None)
        return
    data = _read_editable_yaml(secrets_file)
    if data is not None and not isinstance(data, dict):
        raise _refuse_not_a_mapping(secrets_file)
    value = data.get(_SIGNING_KEY_ENTRY) if isinstance(data, dict) else None
    if value is not None and _tag_of(value) != "!file":
        # An inline PEM block, in the one wording this package already
        # has for it — the shape is the same question wherever it is met.
        from mcuhome.workbench.signing import refuse_inline_key

        stated = refuse_inline_key(secrets_file)
        raise _refused(
            stated.message,
            location=Location(file=secrets_file, key=_SIGNING_KEY_ENTRY),
            hint=stated.hint or "",
        )

    referenced = _referenced_name(data)
    if referenced is None:
        _check_unreferenced_key(directory, referenced=None)
        return
    if _is_outside(referenced):
        return  # a key the user keeps elsewhere; that path is theirs
    key = directory / referenced
    if key.is_file():
        if referenced != _SIGNING_KEY_FILE and (directory / _SIGNING_KEY_FILE).exists():
            raise _refuse_two_keys(key, directory / _SIGNING_KEY_FILE)
        _check_unreferenced_key(directory, referenced=referenced)
        return
    if (directory / _SIGNING_KEY_FILE).is_file():
        # The one half-done state a crash can leave: the key file was
        # renamed and the reference was not rewritten. Completed below.
        _check_unreferenced_key(directory, referenced=_SIGNING_KEY_FILE)
        return
    raise _refuse_dangling_reference(secrets_file, key)


def _check_unreferenced_key(directory: Path, *, referenced: str | None) -> None:
    """Key material in *directory* that this migration would leave behind.

    Everything MCUHome wrote itself is adopted (``key.pem``,
    ``mcuboot.pem``); a private key under any other name is refused here
    rather than passed through, because a project that keeps it ends up
    refused by signing instead — at a point where the upgrade it would be
    told to run has already happened.
    """
    known = {referenced} if referenced else set()
    known.update(_ADOPTABLE_KEYS)
    other = _key_material(directory, ignoring=known)
    if other is not None:
        raise _refuse_other_key_material(other)


def _key_material(directory: Path, *, ignoring: set[str]) -> Path | None:
    """The first file in *directory* holding a private key, by name order."""
    # Imported inside the call, for the reason :func:`_read_editable_yaml`
    # gives.
    from mcuhome.workbench.signing import is_p256_private_key

    for entry in sorted(directory.iterdir()):
        if entry.name in ignoring or not entry.is_file() or entry.is_symlink():
            continue
        try:
            text = entry.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # unreadable or binary: nothing this can judge
        if is_p256_private_key(text):
            return entry
    return None


_UPGRADE_AGAIN = "then run the upgrade again:\n    mcuhome project upgrade"


def _refused(message: str, *, location: Location, hint: str) -> Exception:
    """Every preflight refusal, in the one type that says "nothing moved".

    Imported inside the call, for the reason :func:`_read_editable_yaml`
    gives. The type is what the upgrade reads: a refusal from the look
    this migration takes before its first write puts the project back
    instead of leaving it marked mid-upgrade.
    """
    from mcuhome.workbench.projectupgrade import MigrationRefused

    return MigrationRefused(message, location=location, hint=hint)


def _refuse_link(path: Path) -> Exception:
    return _refused(
        f"MCUHome will not move the secrets through a link: {path} is one.",
        location=Location(file=path),
        hint=(
            "moving files into or out of a link would put them somewhere this "
            "project cannot see. Replace the link with a real directory (or move "
            f"its content here yourself), {_UPGRADE_AGAIN}"
        ),
    )


def _refuse_not_a_directory(path: Path) -> Exception:
    return _refused(
        f"{path} is a file, and the upgrade needs it to be a directory of secrets.",
        location=Location(file=path),
        hint=f"move that file out of secrets/ or rename it, {_UPGRADE_AGAIN}",
    )


def _refuse_two_signing_directories(old: Path, new: Path) -> Exception:
    return _refused(
        f"This project keeps signing secrets in two directories: {old} and {new}.",
        location=Location(file=old),
        hint=(
            "MCUHome will not decide which of them is the project's — a device only "
            "accepts images signed with the key its bootloader carries. Keep one, "
            f"move anything you still need into it, {_UPGRADE_AGAIN}"
        ),
    )


def _refuse_two_files(old: Path, new: Path) -> Exception:
    return _refused(
        f"The signing directory holds two files for the same thing: {old.name} and {new.name}.",
        location=Location(file=old),
        hint=(
            f"{old.name} is what this file used to be called and {new.name} is what it "
            f"is called now, so MCUHome cannot tell which one counts. In {old.parent}, "
            f"keep the one you use and remove the other, {_UPGRADE_AGAIN}"
        ),
    )


def _refuse_two_keys(referenced: Path, other: Path) -> Exception:
    return _refused(
        f"This project holds two signing keys: {referenced} (the one it points at) and {other}.",
        location=Location(file=other),
        hint=(
            "a project that holds two cannot say which key its devices were "
            "bootstrapped with, so MCUHome will not choose. Remove the one you do not "
            f"use, {_UPGRADE_AGAIN}"
        ),
    )


def _refuse_other_key_material(path: Path) -> Exception:
    return _refused(
        f"{path} holds a signing key under a name MCUHome does not use, and nothing points at it.",
        location=Location(file=path),
        hint=(
            "the upgrade would leave it behind and every later command would refuse "
            "over it. If it is this project's signing key, rename it:\n"
            f"    mv {path} {path.parent / _SIGNING_KEY_FILE}\n"
            f"if it is not, move it out of {path.parent}, and {_UPGRADE_AGAIN}"
        ),
    )


def _refuse_dangling_reference(secrets_file: Path, key: Path) -> Exception:
    return _refused(
        f"{secrets_file} points at the signing key {key.name}, and that file is not there.",
        location=Location(file=secrets_file, key=_SIGNING_KEY_ENTRY),
        hint=(
            "the upgrade would carry the reference over and the project still could "
            f"not sign. Put the key file back at {key}, or remove the "
            f"{_SIGNING_KEY_ENTRY} line to let MCUHome draw a new key — which every "
            f"device of this project then has to be bootstrapped with again — and "
            f"{_UPGRADE_AGAIN}"
        ),
    )


def _refuse_not_a_mapping(secrets_file: Path) -> Exception:
    return _refused(
        f"{secrets_file} is not a mapping of `name: value` pairs.",
        location=Location(file=secrets_file, line=1, column=1),
        hint=(
            "the signing secrets file holds one entry per line, "
            f"`{_SIGNING_KEY_ENTRY}: !file {_SIGNING_KEY_FILE}` among them. Repair the "
            f"file, {_UPGRADE_AGAIN}"
        ),
    )


# --------------------------------------------------------------------------
# Moving
# --------------------------------------------------------------------------


def _move_directory(source: Path, target: Path) -> None:
    """Move *source* to *target*, whole where it can be, entry by entry else.

    Both shapes the preflight allows: a target that is not there yet (one
    atomic rename), and a target directory that already holds files of
    its own (each entry that has no counterpart there moves; a name that
    exists in both keeps the file in the new directory and leaves the old
    one where it is, so this directory stays behind holding it).
    """
    if not source.is_dir():
        return
    if not target.exists():
        os.replace(source, target)
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
    """Rename the signing files and make the key reference name key.pem.

    Every shape that reaches here was accepted by the preflight, so this
    only has to be repeatable: each step asks what is on disk, and the
    one interrupted state — the key renamed, the reference not yet
    rewritten — is finished rather than reported.
    """
    if not directory.is_dir():
        return
    for old, new in _RENAMED_BY_NAME:
        _move_file(directory / old, directory / new)

    secrets_file = directory / _SIGNING_YAML
    data = _read_editable_yaml(secrets_file) if secrets_file.is_file() else None
    key_file = directory / _SIGNING_KEY_FILE
    referenced = _referenced_name(data)

    if referenced is not None:
        if referenced == _SIGNING_KEY_FILE or _is_outside(referenced):
            return  # already named right, or a key the user keeps elsewhere
        if (directory / referenced).is_file():
            os.replace(directory / referenced, key_file)
        # Either this run renamed the key or an interrupted one did: the
        # rewrite is what is left, and it is the same write both times.
        _rewrite_reference(secrets_file, data)
        return

    adopted = _unreferenced_key(directory)
    if adopted is None:
        return
    _move_file(adopted, key_file)
    _write_reference(secrets_file, data)


def _signing_secrets_file(directory: Path) -> Path | None:
    """The file that carries the reference, under either of its two names."""
    for name in (_OLD_SIGNING_YAML, _SIGNING_YAML):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _is_outside(referenced: str) -> bool:
    """Whether a reference names something other than a file beside the YAML."""
    return "/" in referenced or "\\" in referenced


def _tag_of(value: Any) -> str | None:
    tag = getattr(value, "tag", None)
    return getattr(tag, "value", None)


def _referenced_name(data: Any) -> str | None:
    """The name behind ``firmware_signing_key: !file <name>``, unfollowed.

    The file is read with the editing parser, so the reference stays a
    tag: the private key it points at is never opened here, and a
    reference to a file that is missing is a fact rather than a failure.
    """
    if not isinstance(data, dict):
        return None
    value = data.get(_SIGNING_KEY_ENTRY)
    if _tag_of(value) != "!file":
        return None
    name = str(getattr(value, "value", "")).strip()
    return name or None


def _unreferenced_key(directory: Path) -> Path | None:
    """Key material in *directory* that no reference names, or ``None``.

    A project whose secrets file lost its entry — or never had one
    because the key was imported by hand — still has the key on disk, and
    the tools refuse to draw a second one beside it. Adopting it here is
    what makes the project usable again. Only the names MCUHome ever
    wrote are adopted; the preflight has already refused anything else.
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


# --------------------------------------------------------------------------
# Reading and writing YAML
# --------------------------------------------------------------------------


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
