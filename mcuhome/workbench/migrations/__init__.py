# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Project migrations: one module per project version (PO 2026-08-16).

A project states which layout it speaks
(:data:`~mcuhome.workbench.projectfile.PROJECT_VERSION`), and when MCUHome
grows past that layout the way forward is a **migration**: one module,
one version step, applied in order by
:mod:`mcuhome.workbench.projectupgrade`. A project three versions behind
runs three of them, in sequence, in one upgrade.

A migration module declares five names and nothing else::

    FROM_VERSION = 0            # the layout it reads
    TO_VERSION = 1              # the layout it produces
    DESCRIPTION = "…"           # one line, for the plan a user approves
    DETAILS = \"\"\"…\"\"\"           # what changed and what to watch out for
    def migrate(root, file): …  # do it; return the new project file

:data:`MIGRATIONS` lists the modules **explicitly**. Discovery by
scanning the directory would make the order an accident of the file
system and would leave a migration out of a wheel that packaged one file
differently; a list in source is a fact, and a test reads it to assert
that the chain runs 0 → 1 → … → ``PROJECT_VERSION`` without a gap, a
duplicate or a step backwards.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from mcuhome.workbench.migrations import v1_project_identity
from mcuhome.workbench.projectfile import ProjectFile

__all__ = ["MIGRATIONS", "Migration", "plan_for"]


@dataclass(frozen=True)
class Migration:
    """One version step, as the runner and the user interface see it."""

    from_version: int
    to_version: int
    #: The module name — what an interrupted upgrade names in its record.
    name: str
    #: One line, imperative: what this step does to the project.
    description: str
    #: The long form: what changed, and what the user has to know
    #: afterwards (a setting that is spelled differently now, a file that
    #: moved). Printed after the upgrade and in ``--dry-run``.
    details: str
    run: Callable[[Path, ProjectFile], ProjectFile]

    @classmethod
    def of(cls, module: ModuleType) -> Migration:
        return cls(
            from_version=module.FROM_VERSION,
            to_version=module.TO_VERSION,
            name=module.__name__.rsplit(".", 1)[-1],
            description=module.DESCRIPTION,
            details=module.DETAILS.strip("\n"),
            run=module.migrate,
        )


#: Every migration, oldest first. Append here when adding one.
MIGRATIONS: tuple[Migration, ...] = (Migration.of(v1_project_identity),)


def plan_for(version: int) -> tuple[Migration, ...]:
    """The migrations that take a project from *version* to the current one."""
    return tuple(migration for migration in MIGRATIONS if migration.from_version >= version)
