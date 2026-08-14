# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Reading and verifying a build context — the compiler's own implementation.

This is deliberately the *second* implementation of the context
read/verify half; :mod:`mcuhome.workbench.contextdir` carries the
client's. The duplication is the contract's own design, not an accident:
build-container-contract.md §3.3 wants every party to compute the
context ID *independently, from the bytes it actually holds* — the
build server recomputes it without the workbench (ADR 0019 §8), the
program inside the container recomputes it without the workbench
(§7.3), and the client computes it when creating the context. What must
be identical everywhere is the *rule*, and the rule lives one package
down, in :mod:`mcuhome.model.context` — both implementations are thin
I/O shells over the same frozen vocabulary, and the golden vectors in
``tests_py`` pin them against each other.

Existing because of ADR 0024: the compiler ships inside the SDK package
and runs in the build container, where the workbench neither exists nor
belongs. Everything here depends on ``mcuhome.model`` and a YAML parser,
nothing else.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML, YAMLError

from mcuhome.model.context import (
    BACKEND_DIR,
    CONTEXT_FILE,
    CONTEXT_VERSION,
    MANIFEST_FILE,
    ContextFile,
    ContextManifest,
    context_id,
    validate_manifest,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.hashes import sha256_file

__all__ = [
    "ContextFormatVersionError",
    "ContextVerification",
    "FileMismatch",
    "read_context_manifest",
    "verify_context",
]


class ContextFormatVersionError(BuildError):
    """The manifest states a ``context`` format version nothing here implements.

    The compiler-side twin of the workbench's error of the same name —
    the build-container contract answers this refusal differently from
    every other unreadable manifest: ``status: "unsupported"``,
    ``reason: "unsupported.context"``, the found version in
    ``error.details`` (build-container-contract.md §3.2). :attr:`found`
    carries the manifest's ``context`` key verbatim — ``None`` when the
    manifest names no format version at all.
    """

    def __init__(self, message: str, *, hint: str, found: object) -> None:
        super().__init__(message, hint=hint)
        self.found = found


def _context_files(root: Path) -> tuple[ContextFile, ...]:
    """Every regular file under *root* that is context content, hashed.

    Neither context document — ``manifest.yaml`` (the list itself) nor
    ``context.yaml`` (the request, whose never-hashed fields would leak
    into the identity through the back door) — is content, and neither
    is the backend-written ``.mcuhome/`` runtime directory
    (build-container-contract.md §3.2).
    """
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in (MANIFEST_FILE, CONTEXT_FILE) or relative.split("/", 1)[0] == BACKEND_DIR:
            continue
        entries.append(ContextFile(path=relative, sha256=sha256_file(path)))
    entries.sort(key=lambda entry: entry.path)
    return tuple(entries)


def read_context_manifest(path: Path) -> ContextManifest:
    """Load a context ``manifest.yaml``, or refuse in plain language.

    Checks shape and spelling — the format version, every hash, every
    path — but deliberately not truth: whether the declared values match
    the bytes next to the manifest is :func:`verify_context`'s job.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise BuildError(
            f"MCUHome cannot read the context manifest {path}: {error.strerror}.",
            hint=(
                f"a build context carries a {MANIFEST_FILE} at its top level — "
                "point at a directory create_context wrote"
            ),
        ) from error
    try:
        data = YAML(typ="safe").load(text)
    except YAMLError as error:
        problem = str(error).splitlines()[0] if str(error) else "unreadable syntax"
        raise BuildError(
            f"The context manifest {path} is not valid YAML ({problem}).",
            hint="it is builder output — recreate the context rather than editing it",
        ) from error
    if not isinstance(data, dict):
        raise BuildError(
            f"The context manifest {path} does not describe a context.",
            hint="it is builder output — recreate the context rather than editing it",
        )

    found = data.get("context")
    if found != CONTEXT_VERSION:
        raise ContextFormatVersionError(
            f"The context manifest {path} has format version {found!r}, and this "
            f"builder implements version {CONTEXT_VERSION}.",
            hint=(
                "the context format is a versioned contract: a mismatch is a refusal "
                "that names both numbers, never a guess. Recreate the context with a "
                "matching mcuhome version."
            ),
            found=found,
        )
    try:
        manifest = ContextManifest.from_dict(data)
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise BuildError(
            f"The context manifest {path} is missing something this builder needs: {error}.",
            hint=(
                f"it states context format {CONTEXT_VERSION}, so this is a truncated "
                "or hand-edited file rather than a version mismatch. Recreate the "
                "context."
            ),
        ) from error
    validate_manifest(manifest)
    return manifest


@dataclass(frozen=True)
class FileMismatch:
    """One file whose declared and actual state disagree.

    Three shapes: a tampered file carries both hashes, a listed-but-
    missing file has no actual hash, a present-but-unlisted file has no
    declared one. All three break the context ID, because the ID is
    recomputed from what is actually there.
    """

    path: str
    declared_sha256: str | None
    actual_sha256: str | None

    def describe(self) -> str:
        if self.declared_sha256 is None:
            return f"{self.path}: present but not in the integrity list"
        if self.actual_sha256 is None:
            return f"{self.path}: listed in the manifest but missing"
        return (
            f"{self.path}: hashes to {self.actual_sha256}, the manifest says {self.declared_sha256}"
        )


@dataclass(frozen=True)
class ContextVerification:
    """What :func:`verify_context` found, declared versus actual."""

    root: Path
    manifest: ContextManifest
    #: The ID of the context as it actually is: recomputed from the
    #: declared pins and the file hashes measured on disk.
    actual_id: str
    mismatches: tuple[FileMismatch, ...]

    @property
    def declared_id(self) -> str:
        return self.manifest.id

    @property
    def ok(self) -> bool:
        return not self.mismatches and self.declared_id == self.actual_id

    def problems(self) -> list[str]:
        """Every disagreement as one plain sentence, file order first."""
        messages = [mismatch.describe() for mismatch in self.mismatches]
        if self.declared_id != self.actual_id:
            messages.append(
                f"context id: the bytes present hash to {self.actual_id}, "
                f"the manifest says {self.declared_id}"
            )
        return messages


def verify_context(root: Path) -> ContextVerification:
    """Recompute every hash and the context ID from the bytes present.

    The in-container/backend primitive behind "never trust declared
    hashes": the manifest's values are advisory, the bytes decide. Every
    file is re-hashed, files the manifest forgot and files it invents
    are both mismatches, and :attr:`~ContextVerification.actual_id` is
    the ID of the context as received. Raises
    :class:`~mcuhome.model.errors.BuildError` only when there is nothing
    to verify against: no manifest, or one too malformed to state
    declared values at all.
    """
    root = Path(root)
    manifest = read_context_manifest(root / MANIFEST_FILE)
    present = _context_files(root)
    declared = {entry.path: entry.sha256 for entry in manifest.files}
    actual = {entry.path: entry.sha256 for entry in present}
    mismatches = tuple(
        FileMismatch(
            path=path,
            declared_sha256=declared.get(path),
            actual_sha256=actual.get(path),
        )
        for path in sorted(set(declared) | set(actual))
        if declared.get(path) != actual.get(path)
    )
    return ContextVerification(
        root=root,
        manifest=manifest,
        actual_id=context_id(
            sdk_sha256=manifest.sdk.sha256,
            board=manifest.board,
            files=present,
        ),
        mismatches=mismatches,
    )
