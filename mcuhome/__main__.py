# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Command line entry point for the MCUHome builder (scaffold)."""

from __future__ import annotations

import sys

from mcuhome import __version__


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--version" in args:
        print(f"mcuhome {__version__}")
        return 0
    print(
        "mcuhome: the MCUHome firmware builder is not implemented yet.\n"
        "Follow progress at https://github.com/mcu-home/mcuhome"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
