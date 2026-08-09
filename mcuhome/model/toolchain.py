# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Zephyr line and binary-blob resolution — the ADR 0013 seam.

ADR 0013 (with its per-blob amendment) describes real machinery: a
per-board blob availability matrix extracted from the builder container
images, a resolver that trades a Zephyr release line against blob
compatibility, and a drift check that flags overrides which have become
redundant.

**None of that exists yet, and this module does not pretend otherwise.**
What it provides is the seam:

* the vocabulary of ``device.blob_usage`` / ``device.zephyr_version`` /
  ``device.blobs`` is accepted and validated (that part is stable —
  yaml-schema.md §3);
* :func:`available_blobs` is the single hook the future availability
  matrix plugs into. Today it returns nothing for every board, because
  MCUHome integrates no blob yet (the MPSL/SDC and nrf_cc3xx feasibility
  work of ADR 0013 §3 is still open);
* :data:`SUPPORTED_ZEPHYR_LINES` is the single place the "at most two
  concurrent lines" rule will grow into. Today there is exactly one.

A user who forces something this cannot deliver gets the plain-language
refusal ADR 0013 asks for, not a silent downgrade.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcuhome.model.errors import ErrorCollector, Location

__all__ = [
    "SUPPORTED_ZEPHYR_LINES",
    "ResolvedToolchain",
    "available_blobs",
    "resolve_toolchain",
]

#: Zephyr release lines this MCUHome release can build (ADR 0008/0013:
#: a line, never a frozen point release — patch releases with security
#: backports are always taken). CHIP v1.5.1.0 is pinned against 4.4.
SUPPORTED_ZEPHYR_LINES: tuple[str, ...] = ("4.4",)

#: The newest supported line; what ``zephyr_version: latest`` resolves to.
LATEST_ZEPHYR_LINE = SUPPORTED_ZEPHYR_LINES[-1]


@dataclass(frozen=True)
class ResolvedToolchain:
    """What the builder will actually build against."""

    #: Resolved Zephyr release line, e.g. ``"4.4"``.
    zephyr_line: str
    #: Resolved value of ``device.blob_usage``.
    blob_usage: str
    #: Resolved per-blob decisions. Empty until blobs are integrated.
    blobs: dict[str, str] = field(default_factory=dict)


def available_blobs(board: str) -> dict[str, str]:
    """Blobs applicable to *board*, by name, with their source.

    The ADR 0013 availability matrix hooks in here. It is empty today:
    MCUHome integrates no vendor blob yet, on any board.
    """
    del board
    return {}


def resolve_toolchain(
    *,
    board: str | None,
    blob_usage: str | None,
    zephyr_version: str | None,
    blobs: dict[str, str],
    blob_locs: dict[str, Location],
    version_loc: Location,
    errors: ErrorCollector,
) -> ResolvedToolchain:
    """Resolve the Zephyr line and blob set, or record why it cannot be."""
    resolved_usage = blob_usage or "auto"

    requested = zephyr_version or "auto"
    if requested in ("auto", "latest"):
        line = LATEST_ZEPHYR_LINE
    elif requested in SUPPORTED_ZEPHYR_LINES:
        line = requested
    else:
        errors.add(
            f"MCUHome cannot build against Zephyr {requested}.",
            location=version_loc,
            hint=(
                "this release builds against Zephyr "
                f"{', '.join(SUPPORTED_ZEPHYR_LINES)} only — remove the "
                "zephyr_version: line and let MCUHome choose:\n"
                "    zephyr_version: auto"
            ),
        )
        line = LATEST_ZEPHYR_LINE

    integrated = available_blobs(board or "")
    for name, decision in blobs.items():
        if decision != "enabled":
            # "disabled" and "auto" are honest no-ops while nothing is
            # integrated: nothing is being switched off, and "auto" is
            # defined to self-heal once a compatible blob appears.
            continue
        if name not in integrated:
            errors.add(
                f'MCUHome cannot enable the "{name}" binary blob: no vendor blob is '
                "integrated yet.",
                location=blob_locs.get(name, version_loc),
                hint=(
                    "blob support is still being evaluated (ADR 0013) — remove the "
                    f"blobs: entry for now:\n    # {name}: enabled"
                ),
            )

    return ResolvedToolchain(zephyr_line=line, blob_usage=resolved_usage, blobs={})
