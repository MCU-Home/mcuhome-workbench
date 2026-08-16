# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``mcuhome device matter-pairing --new``: draw commissioning credentials, once.

This is the one place in the builder where randomness enters, and it
enters *into the user's configuration* rather than into a build. That is
the whole design (yaml-schema.md §4.1):

* the security goal is a passcode and a salt nobody else has — a shared
  passcode lets anyone commission the device, and a shared salt lets one
  precomputed table unlock every device ever built (IACR 2025/1268);
* the builder's invariant is that the same configuration produces the
  same bytes forever, which rules out drawing credentials at build time;

so the credentials are drawn once, written down, and are ordinary
configuration input from then on. Nothing is stored anywhere else: no
state directory, no cache, no log file. The values are security-relevant
and ``main.yaml`` is the file a project commits, so they never land
there (PO 2026-08-15): ``main.yaml`` gets ``!secret`` references, the
values themselves go to the device's own ``secrets/devices/<name>.yaml``
— the per-device rung of the loader's secrets ladder.

**Matter has to be on.** Under the explicit-opt-in default (PO
2026-08-15, yaml-schema.md §4) an absent ``matter:`` block means Matter
is off, and a command that silently switched a protocol on while writing
credentials would decide something the configuration's author did not —
so both the absent block and ``enabled: false`` are refusals that say
what to write.

**The file is edited, not rewritten.** A YAML round-trip that
re-serializes the document reflows indentation and moves comments, which
is not an acceptable thing to do to a file somebody wrote by hand. The
parse tree is used only to find the right line; the edit itself is
textual, so every byte the user typed elsewhere is still there
afterwards.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model import pairing
from mcuhome.model.errors import ConfigError, Location
from ruamel.yaml import YAML

from mcuhome.workbench.loader import device_secrets_file, load_yaml_file
from mcuhome.workbench.validate import PAIRING_KEYS

__all__ = ["CREDENTIAL_COMMENT", "PairingResult", "init_pairing", "secret_names"]

#: Written above the credentials, and recognized again when ``--force``
#: replaces them so that repeated runs do not stack up comment blocks.
#: Matched by exact text — a comment the user wrote is never touched.
CREDENTIAL_COMMENT = (
    "# Commissioning credentials, written by `mcuhome device matter-pairing`.",
    "# They are this device's identity: replacing them means every controller",
    "# has to commission it again.",
)

#: Comment lines earlier versions of this command wrote, still recognized
#: when replacing so that a pre-rename file does not stack comments up.
_KNOWN_COMMENT_LINES = frozenset(CREDENTIAL_COMMENT) | {
    "# Commissioning credentials, written by `mcuhome device init-pairing`.",
}


@dataclass(frozen=True)
class PairingResult:
    """What ``matter-pairing --new`` wrote, and where."""

    entry: Path
    #: Where the values themselves went: the device's own secrets file.
    secrets_file: Path
    pairing: pairing.Pairing
    #: True when credentials were already there and ``--force`` replaced them.
    replaced: bool


def secret_names() -> dict[str, str]:
    """The secrets-file key each credential gets.

    Plain, unprefixed names: the file is the device's own
    (``secrets/devices/<name>.yaml``), so there is nothing to
    disambiguate — the old per-device slug prefix retired with the
    shared secrets file (PO 2026-08-15).
    """
    return {key: f"matter_{key}" for key in PAIRING_KEYS}


# --------------------------------------------------------------------------
# A file as lines, so an edit can be made by line number
# --------------------------------------------------------------------------


@dataclass
class _Text:
    lines: list[str]
    newline: str
    trailing_newline: bool

    @staticmethod
    def of(text: str) -> _Text:
        return _Text(
            lines=text.splitlines(),
            newline="\r\n" if "\r\n" in text else "\n",
            trailing_newline=text.endswith(("\n", "\r")) or not text,
        )

    def render(self) -> str:
        body = self.newline.join(self.lines)
        return body + self.newline if self.trailing_newline and body else body

    def indent_of(self, line_number: int) -> int:
        line = self.lines[line_number - 1]
        return len(line) - len(line.lstrip())

    def block_end(self, key_line: int, key_column: int) -> int:
        """Last line belonging to the block opened at *key_line* (1-based).

        A line belongs to it while it is blank or indented deeper than the
        key that opened it.
        """
        last = key_line
        for number in range(key_line + 1, len(self.lines) + 1):
            if not self.lines[number - 1].strip():
                continue
            if self.indent_of(number) <= key_column:
                break
            last = number
        return last


# --------------------------------------------------------------------------
# Finding the place in network.matter
# --------------------------------------------------------------------------


def _mapping(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _key_line(mapping: Any, key: str) -> int:
    return int(mapping.lc.key(key)[0]) + 1


def _key_column(mapping: Any, key: str) -> int:
    return int(mapping.lc.key(key)[1])


def _refuse(message: str, entry: Path, hint: str, line: int | None = None) -> ConfigError:
    return ConfigError(message, location=Location(file=entry, line=line), hint=hint)


_FLOW_MATTER_HINT = (
    "write it as a block before running this command:\n    matter:\n      enabled: true"
)


@dataclass(frozen=True)
class _Anchor:
    """Where the credentials go, and what is already in the way."""

    #: 1-based line the new lines are inserted after.
    after_line: int
    #: Column the credential keys are written at.
    indent: int
    #: Lines that have to go first, by the key that occupies them.
    occupied: dict[str, int]


def _find_anchor(data: Any, text: _Text, entry: Path) -> _Anchor:
    root = _mapping(data)
    network = _mapping(root.get("network")) if root else None
    if network is None or not network:
        raise _refuse(
            "This device has no network: section, so there is nothing to commission it onto.",
            entry,
            "commissioning is how a Matter device joins a network — give the device a "
            "transport first:\n    network:\n      thread:\n        device_role: ftd",
        )

    if "matter" not in network:
        # Under the explicit-opt-in default (PO 2026-08-15) an absent
        # block means Matter is off — writing one here would switch a
        # protocol on behind the author's back.
        raise _refuse(
            "Matter is off for this device (there is no matter: section), so there "
            "is nothing to draw commissioning credentials for.",
            entry,
            f"switch it on first — {_FLOW_MATTER_HINT}",
            line=_key_line(root, "network"),
        )

    matter_line = _key_line(network, "matter")
    matter_column = _key_column(network, "matter")
    matter = _mapping(network.get("matter"))
    if matter is None:
        # `matter:` with nothing under it: the credentials become its
        # first keys, at the conventional two-space indent.
        return _Anchor(
            after_line=matter_line,
            indent=matter_column + 2,
            occupied={},
        )
    if not matter or _key_column(matter, next(iter(matter))) <= matter_column:
        raise _refuse(
            "The matter: section is written on one line, which leaves nowhere to add to.",
            entry,
            _FLOW_MATTER_HINT,
            line=matter_line,
        )

    if matter.get("enabled") is False:
        raise _refuse(
            "Matter is switched off for this device, so it is never commissioned.",
            entry,
            "remove enabled: false (or set it to true) if this device should join a Matter fabric",
            line=_key_line(matter, "enabled"),
        )

    occupied = {
        key: _key_line(matter, key) for key in (*PAIRING_KEYS, "use_test_pairing") if key in matter
    }
    return _Anchor(
        # After everything already under `matter:`, so the credentials
        # read as an addition to the section rather than as an
        # interruption of it.
        after_line=text.block_end(matter_line, matter_column),
        indent=_key_column(matter, next(iter(matter))),
        occupied=occupied,
    )


def _lines_to_drop(text: _Text, occupied: dict[str, int]) -> set[int]:
    """The occupied lines, their continuations, and our own comment above."""
    drop: set[int] = set()
    for line_number in occupied.values():
        column = text.indent_of(line_number)
        for number in range(line_number, text.block_end(line_number, column) + 1):
            drop.add(number)
    for number in range(min(occupied.values()) - 1, 0, -1):
        if text.lines[number - 1].strip() not in _KNOWN_COMMENT_LINES:
            break
        drop.add(number)
    return drop


# --------------------------------------------------------------------------
# The command
# --------------------------------------------------------------------------


def init_pairing(
    entry: Path,
    *,
    secrets_file: Path,
    force: bool = False,
    draw: Callable[[], pairing.Pairing] = pairing.random_pairing,
) -> PairingResult:
    """Write fresh commissioning credentials for the configuration *entry*.

    *secrets_file* is the project's **main** secrets file; the values
    themselves go to the device's own file next to it
    (:func:`~mcuhome.workbench.loader.device_secrets_file`), and the
    configuration gets ``!secret`` references — the values are
    security-relevant and never land in the committable file (PO
    2026-08-15).

    *draw* is the source of randomness, injected so the tests can pin it.
    Everything else about this function is deterministic.
    """
    text = _Text.of(entry.read_text(encoding="utf-8"))
    data = load_yaml_file(entry)
    anchor = _find_anchor(data, text, entry)

    if anchor.occupied and not force:
        names = ", ".join(f"{key}:" for key in sorted(anchor.occupied))
        raise _refuse(
            "This device already has commissioning credentials.",
            entry,
            f"{names} would be replaced, and every controller that already knows this "
            "device would have to commission it again — so say it in so many words:\n"
            "    mcuhome device matter-pairing --new … --force",
            line=min(anchor.occupied.values()),
        )

    credentials = draw()
    names = secret_names()
    device_file = device_secrets_file(secrets_file, data, entry=entry)

    after_line = anchor.after_line
    if anchor.occupied:
        dropped = _lines_to_drop(text, anchor.occupied)
        after_line -= sum(1 for number in dropped if number <= after_line)
        text.lines = [
            line for number, line in enumerate(text.lines, start=1) if number not in dropped
        ]

    # The secrets file first: if it refuses (a clash without --force),
    # the configuration has not been touched yet.
    _write_secrets(device_file, names, credentials, force=force)
    text.lines[after_line:after_line] = _credential_lines(anchor, names)
    entry.write_text(text.render(), encoding="utf-8")

    return PairingResult(
        entry=entry,
        secrets_file=device_file,
        pairing=credentials,
        replaced=bool(anchor.occupied),
    )


def _credential_lines(anchor: _Anchor, names: dict[str, str]) -> list[str]:
    pad = " " * anchor.indent
    out = [f"{pad}{line}" for line in CREDENTIAL_COMMENT]
    out += [f"{pad}{key}: !secret {names[key]}" for key in PAIRING_KEYS]
    return out


def _write_secrets(
    secrets_file: Path,
    names: dict[str, str],
    credentials: pairing.Pairing,
    *,
    force: bool,
) -> None:
    """Append the three values to the device's secrets file, replacing old ones.

    Line editing again, and for the same reason: a secrets file is a file
    a person keeps, not a database this command owns.
    """
    values = {
        names["discriminator"]: str(credentials.discriminator),
        names["passcode"]: str(credentials.passcode),
        names["salt"]: f'"{credentials.salt}"',
    }
    created = not secrets_file.is_file()
    if not created:
        raw = secrets_file.read_text(encoding="utf-8")
        text = _Text.of(raw)
        existing = YAML(typ="safe").load(raw) or {}
        clashing = sorted(name for name in values if name in existing)
        if clashing and not force:
            raise _refuse(
                f"{secrets_file.name} already has a secret called {clashing[0]}.",
                secrets_file,
                "this device's secrets file already carries commissioning values — "
                "replacing them means every controller that knows the device has to "
                "commission it again, so say it with --force",
            )
        text.lines = [line for line in text.lines if line.split(":", 1)[0].strip() not in values]
    else:
        text = _Text.of("")
        text.lines = [
            "# Secrets for one device: `!secret <name>` in its configuration reads",
            "# from here first, then from the project's secrets/main.yaml. Keep this",
            "# file out of version control.",
        ]
    if text.lines and text.lines[-1].strip():
        text.lines.append("")
    text.lines += [f"{name}: {value}" for name, value in values.items()]
    if created:
        # A fresh secrets file starts owner-only, and so does any missing
        # directory on the way to it (ADR 0022 §5) — exactly the missing
        # ones (secrets/devices/ typically): an existing directory's
        # permissions are the user's. Rewriting an existing file goes
        # through its inode and keeps whatever the user set.
        missing: list[Path] = []
        probe = secrets_file.parent
        while not probe.exists():
            missing.append(probe)
            probe = probe.parent
        secrets_file.parent.mkdir(parents=True, exist_ok=True)
        for directory in missing:
            os.chmod(directory, 0o700)
        descriptor = os.open(
            secrets_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text.render())
    else:
        secrets_file.write_text(text.render(), encoding="utf-8")
