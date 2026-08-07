# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Command line entry point for the MCUHome builder."""

from __future__ import annotations

from mcuhome.cli import main

__all__ = ["main"]

if __name__ == "__main__":
    raise SystemExit(main())
