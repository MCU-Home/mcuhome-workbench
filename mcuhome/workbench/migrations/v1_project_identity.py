# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""0 → 1: the project file gains a version and a unique identity."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from mcuhome.workbench.projectfile import ProjectFile, new_project_id

FROM_VERSION = 0
TO_VERSION = 1

DESCRIPTION = "Give the project a version and a unique identity"

DETAILS = """
.mcuhome-project-root now holds two values instead of only a comment:
the project version, so MCUHome can tell whether it speaks this
project's layout, and a unique project id that is drawn once and never
changes.

What this means for you:

  * The file is TOML now, and MCUHome maintains it. Do not edit it by
    hand — a wrong value here can make the whole project unusable.
  * Keep it in version control. The id is part of the project, not of
    this machine, so a clone of the project is the same project.
  * Your configuration is untouched: devices/, secrets/ and
    mcuhome.yaml are exactly as they were.

Commands that identify a project — the upgrade's own confirmation, for
one — accept the last six characters of the id as a short form.
"""


def migrate(root: Path, file: ProjectFile) -> ProjectFile:
    """Draw the project's id. The version itself is the runner's to write."""
    return replace(file, id=file.id or new_project_id())
