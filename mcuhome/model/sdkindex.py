# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The SDK package index — shared names, and nothing else.

The same source directory is read by whoever resolves a pin (the
workbench, E65) and by whoever fetches and verifies the bytes (a
backend, contract §9.1). The two must agree on what the index file and
the package are *called*; everything they do with them is deliberately
their own — the workbench resolves constraints, a backend re-reads the
index for one exact version and lets the hash decide. Two constants are
vocabulary, so they live here; code stays out, because this package is
identical everywhere and does no I/O (ADR 0020 decision 1).
"""

from __future__ import annotations

__all__ = ["INDEX_FILE", "SDK_PACKAGE_NAME"]

#: The per-source-directory index ``scripts/build_sdk_archive.py`` writes.
INDEX_FILE = "index.json"

#: The one package name the SDK ships under (``mcuhome-sdk-<version>.tar.zst``).
SDK_PACKAGE_NAME = "mcuhome-sdk"
