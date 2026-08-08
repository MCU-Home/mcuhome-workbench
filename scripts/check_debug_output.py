#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Debug output is load-bearing — lint for silently reduced diagnostics.

Enforces the product-owner directive recorded in AGENTS.md ("Debug
output is load-bearing"): no config fragment in this repository may
disable or reduce debug output silently. The precedent is ADR 0015's RTT
amendment — an OTA swap failure whose one explanatory MCUboot log line
was compiled out cost a day of source reading.

What it scans:

* every ``*.conf`` file in the repository (prj.conf, snippet fragments,
  board fragments, test fragments), and
* Python sources of the ``mcuhome`` package, where the registry keeps
  the per-board config fragments as string tuples
  (``mcuhome/registry.py``) — there the forbidden line must appear
  inside a string literal to count, so prose comments discussing a
  symbol never trip the check.

What it skips (host-side or generated, annotating them would be wrong):

* ``tests_py/`` — pytest fixtures and the golden files, which are
  byte-compared generator output and must never be hand-edited; their
  source of truth is ``mcuhome/registry.py``, which IS scanned;
* ``docs/``, ``containers/``, ``scripts/``, ``patches/``, ``.github/``,
  plus build output (``build/``, ``twister-out*``).

A hit passes only with an explicit approval marker on the same line or
the directly preceding line::

    # debug-output: approved <short reason, or where the decision is recorded>

The marker states a decision; it never creates one (AGENTS.md).

Exit status: 0 when clean, 1 with findings (one ``file:line`` per
finding), 2 on usage errors.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

#: Marker that records an approved, documented reduction. The reason is
#: mandatory: a bare "approved" with nothing to trace is itself a defect.
MARKER_RE = re.compile(r"#\s*debug-output:\s*approved\s+\S")

#: Kconfig assignments that reduce or remove diagnostics. Anchored to a
#: full assignment so that prose, symbol names without values, and
#: unrelated symbols sharing a prefix never match.
_FORBIDDEN = [
    r"CONFIG_LOG=n",
    r"CONFIG_LOG_MODE_MINIMAL=y",
    r"CONFIG_USE_SEGGER_RTT=n",
    r"CONFIG_LOG_BACKEND_RTT=n",
    r"CONFIG_RTT_CONSOLE=n",
    r"CONFIG_UART_CONSOLE=n",
    r"CONFIG_CONSOLE=n",
    r"CONFIG_BOOT_BANNER=n",
    r"CONFIG_PRINTK=n",
    r"CONFIG_LOG_PRINTK=n",
    r"CONFIG_ASSERT=n",
    r"CONFIG_LOG_DEFAULT_LEVEL=[01]",
    r"CONFIG_LOG_MAX_LEVEL=[01]",
    r"CONFIG_MCUBOOT_LOG_LEVEL_OFF=y",
    r"CONFIG_MCUBOOT_LOG_LEVEL_ERR=y",
    r"CONFIG_MCUHOME_RTT_REINIT=n",
    r"CONFIG_THREAD_NAME=n",
]

#: In a .conf fragment the assignment is the whole (stripped) line.
CONF_HIT_RE = re.compile(r"^(?:" + "|".join(_FORBIDDEN) + r")\s*$")

#: In Python sources the assignment must sit in a string literal — that
#: is how mcuhome/registry.py carries the fragments it generates.
PY_HIT_RE = re.compile(r"[\"'](?:" + "|".join(_FORBIDDEN) + r")[\"']")

#: Directories never scanned, by name, at any depth.
EXCLUDE_DIRS = {
    ".git",
    ".github",
    ".west",
    "__pycache__",
    "build",
    "containers",
    "docs",
    "patches",
    "scripts",
    "tests_py",
}


def _excluded(path: Path, root: Path) -> bool:
    parts = path.relative_to(root).parts[:-1]
    return any(part in EXCLUDE_DIRS or part.startswith("twister-out") for part in parts)


def _approved(lines: list[str], index: int) -> bool:
    """True if line ``index`` carries the marker or the line above does."""
    if MARKER_RE.search(lines[index]):
        return True
    return index > 0 and bool(MARKER_RE.search(lines[index - 1]))


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    conf_files = sorted(p for p in root.rglob("*.conf") if not _excluded(p, root))
    py_files = sorted(p for p in (root / "mcuhome").rglob("*.py") if not _excluded(p, root))

    for path, hit_re, comment_ok in [
        *((p, CONF_HIT_RE, False) for p in conf_files),
        *((p, PY_HIT_RE, True) for p in py_files),
    ]:
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, raw in enumerate(lines):
            line = raw.strip()
            if comment_ok and line.startswith("#"):
                continue  # a Python comment quoting a symbol is prose, not config
            if (hit_re.search if comment_ok else hit_re.match)(line) and not _approved(lines, i):
                findings.append(f"{path.relative_to(root)}:{i + 1}: {line}")
    return findings


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(f"usage: {argv[0]} [repo-root]", file=sys.stderr)
        return 2
    root = Path(argv[1]).resolve() if len(argv) == 2 else Path(__file__).resolve().parents[1]

    findings = scan(root)
    if not findings:
        return 0

    print("Silently reduced debug output (AGENTS.md: 'Debug output is load-bearing'):\n")
    for finding in findings:
        print(f"  {finding}")
    print(
        "\nDebug output stays in every image until v1.0 (product-owner directive).\n"
        "If this reduction is a recorded, deliberate decision, say so on the same\n"
        "or the directly preceding line:\n"
        "    # debug-output: approved <short reason, or where the decision is recorded>\n"
        "If it is not recorded anywhere, do not add the marker — report the space\n"
        "or design pressure and get the decision made first."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
