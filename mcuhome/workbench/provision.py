# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""``mcuhome init-pairing``: draw commissioning credentials, once.

This is the one place in the builder where randomness enters, and it
enters *into the user's configuration file* rather than into a build.
That is the whole design (yaml-schema.md §4.1):

* the security goal is a passcode and a salt nobody else has — a shared
  passcode lets anyone commission the device, and a shared salt lets one
  precomputed table unlock every device ever built (IACR 2025/1268);
* the builder's invariant is that the same configuration produces the
  same bytes forever, which rules out drawing credentials at build time;

so the credentials are drawn once, written down, and are ordinary
configuration input from then on. Nothing is stored anywhere else: no
state directory, no cache, no log file. The configuration the user backs
up *is* the record, and ``--secrets`` moves the values into the tree's
``secrets.yaml`` for configurations that live in version control.

**The file is edited, not rewritten.** A YAML round-trip that
re-serializes the document reflows indentation and moves comments, which
is not an acceptable thing to do to a file somebody wrote by hand. The
parse tree is used only to find the right line; the edit itself is
textual, so every byte the user typed elsewhere is still there
afterwards.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcuhome.model import pairing
from mcuhome.model.errors import ConfigError, Location
from ruamel.yaml import YAML

from mcuhome.workbench.loader import load_yaml_file
from mcuhome.workbench.validate import PAIRING_KEYS

__all__ = ["CREDENTIAL_COMMENT", "InitResult", "init_pairing", "secret_names"]

#: Written above the credentials, and recognized again when ``--force``
#: replaces them so that repeated runs do not stack up comment blocks.
#: Matched by exact text — a comment the user wrote is never touched.
CREDENTIAL_COMMENT = (
    "# Commissioning credentials, written by `mcuhome init-pairing`.",
    "# They are this device's identity: replacing them means every controller",
    "# has to commission it again.",
)


@dataclass(frozen=True)
class InitResult:
    """What ``init-pairing`` wrote, and where."""

    entry: Path
    #: Where the values themselves went, when ``--secrets`` was used.
    secrets_file: Path | None
    pairing: pairing.Pairing
    #: True when credentials were already there and ``--force`` replaced them.
    replaced: bool


def secret_names(device: str) -> dict[str, str]:
    """The ``secrets.yaml`` key each credential gets for *device*."""
    slug = "".join(character if character.isalnum() else "_" for character in device.lower())
    return {key: f"{slug}_{key}" for key in PAIRING_KEYS}


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
    #: True when the ``matter:`` key itself has to be written too.
    needs_matter_key: bool
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
        # No matter: section at all — write one at the end of network:.
        network_line = _key_line(root, "network")
        return _Anchor(
            after_line=text.block_end(network_line, _key_column(root, "network")),
            indent=_key_column(network, next(iter(network))),
            needs_matter_key=True,
            occupied={},
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
            needs_matter_key=False,
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
        needs_matter_key=False,
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
        if text.lines[number - 1].strip() not in CREDENTIAL_COMMENT:
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
    use_secrets: bool = False,
    force: bool = False,
    draw: Callable[[], pairing.Pairing] = pairing.random_pairing,
) -> InitResult:
    """Write fresh commissioning credentials into the configuration *entry*.

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
            "    mcuhome init-pairing … --force",
            line=min(anchor.occupied.values()),
        )

    credentials = draw()
    names = secret_names(_device_name(data, entry))

    after_line = anchor.after_line
    if anchor.occupied:
        dropped = _lines_to_drop(text, anchor.occupied)
        after_line -= sum(1 for number in dropped if number <= after_line)
        text.lines = [
            line for number, line in enumerate(text.lines, start=1) if number not in dropped
        ]

    text.lines[after_line:after_line] = _credential_lines(
        anchor, credentials, names, use_secrets=use_secrets
    )
    entry.write_text(text.render(), encoding="utf-8")

    if use_secrets:
        _write_secrets(secrets_file, names, credentials, force=force)

    return InitResult(
        entry=entry,
        secrets_file=secrets_file if use_secrets else None,
        pairing=credentials,
        replaced=bool(anchor.occupied),
    )


def _credential_lines(
    anchor: _Anchor,
    credentials: pairing.Pairing,
    names: dict[str, str],
    *,
    use_secrets: bool,
) -> list[str]:
    out: list[str] = []
    pad = " " * anchor.indent
    if anchor.needs_matter_key:
        out.append(f"{pad}matter:")
        pad = f"{pad}  "
    out += [f"{pad}{line}" for line in CREDENTIAL_COMMENT]
    if use_secrets:
        out += [f"{pad}{key}: !secret {names[key]}" for key in PAIRING_KEYS]
    else:
        out += [
            f"{pad}discriminator: {credentials.discriminator}",
            f"{pad}passcode: {credentials.passcode}",
            f'{pad}salt: "{credentials.salt}"',
        ]
    return out


def _device_name(data: Any, entry: Path) -> str:
    root = _mapping(data)
    device = _mapping(root.get("device")) if root else None
    name = device.get("name") if device else None
    return name if isinstance(name, str) and name else entry.parent.name


def _write_secrets(
    secrets_file: Path,
    names: dict[str, str],
    credentials: pairing.Pairing,
    *,
    force: bool,
) -> None:
    """Append the three values to the tree's secrets file, replacing old ones.

    Line editing again, and for the same reason: a secrets file is a file
    a person keeps, not a database this command owns.
    """
    values = {
        names["discriminator"]: str(credentials.discriminator),
        names["passcode"]: str(credentials.passcode),
        names["salt"]: f'"{credentials.salt}"',
    }
    if secrets_file.is_file():
        raw = secrets_file.read_text(encoding="utf-8")
        text = _Text.of(raw)
        existing = YAML(typ="safe").load(raw) or {}
        clashing = sorted(name for name in values if name in existing)
        if clashing and not force:
            raise _refuse(
                f"{secrets_file.name} already has a secret called {clashing[0]}.",
                secrets_file,
                "two devices cannot share commissioning credentials — rename one of "
                "them, or replace the values with --force",
            )
        text.lines = [line for line in text.lines if line.split(":", 1)[0].strip() not in values]
    else:
        text = _Text.of("")
        text.lines = [
            "# Secrets for this configuration tree: `!secret <name>` in a device",
            "# configuration reads from here. Keep this file out of version control.",
        ]
    if text.lines and text.lines[-1].strip():
        text.lines.append("")
    text.lines += [f"{name}: {value}" for name, value in values.items()]
    secrets_file.write_text(text.render(), encoding="utf-8")
