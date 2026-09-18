# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The surface against its reference: ``api.__all__`` versus ``docs/api.md``.

The reference is the contract — every exported name with its signature
lives there, and a consumer reads it rather than the source. A reference
and a surface that are written by hand in two places drift apart within a
release, and the drift is invisible: nothing fails, the document simply
starts lying. So this module reads the document and asserts the package
against it.

**What is parsed, and how.** Two things, both by a rule stated here so
that a later reader can keep the document parseable:

* *The index.* The section ``## Index of exported names`` ends the
  document. Its group paragraphs each begin with a bold label
  (``**Constants** — ...``); every name in backticks inside those
  paragraphs is an exported name. The section's own introductory
  sentence is not a group paragraph, which is what keeps
  ``mcuhome.workbench.api`` out of the list.
* *The signatures.* In every fenced ``python`` block, a line that starts
  at column zero with ``def`` or ``async def`` opens a signature; it runs
  until its parentheses balance. That is a Python function definition
  with the body left out, so it is parsed with :mod:`ast` rather than
  with expressions of our own. Indented ``def`` lines are methods shown
  in passing and are not part of this check, and ``class`` blocks state
  fields rather than a call, so they are not either.

**The pending list.** The workbench is being rewritten onto this surface
one feature at a time, and the reference states the whole target while
the package holds the part that exists. :data:`PENDING` names what is
still missing and which feature it belongs to. It is the only place that
difference is allowed to be recorded: a name that is exported while
still listed here fails, and so does one that is neither exported nor
listed. The list shrinks as features land and is empty when the surface
is complete.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import re
from typing import Any

import pytest
from conftest import REPO_ROOT

from mcuhome.workbench import api

REFERENCE = REPO_ROOT / "docs" / "api.md"

#: What the reference already promises and the package does not carry
#: yet, by the feature that brings it. Entries leave this list in the
#: same commit that adds the name to ``__all__``; nothing is ever added
#: to it without the name existing in the reference's index, which
#: :func:`test_every_pending_name_is_in_the_index` pins.
PENDING: dict[str, str] = {}


def _python_blocks(text: str) -> list[list[str]]:
    """Every fenced ``python`` block of the reference, as its lines."""
    blocks: list[list[str]] = []
    inside = False
    current: list[str] = []
    for line in text.split("\n"):
        if not inside and line.startswith("```python"):
            inside, current = True, []
        elif inside and line.startswith("```"):
            blocks.append(current)
            inside = False
        elif inside:
            current.append(line)
    return blocks


def _reference_signatures(text: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """The stated signature of every documented callable, by name."""
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for block in _python_blocks(text):
        index = 0
        while index < len(block):
            line = block[index]
            if line.startswith(("def ", "async def ")):
                chunk = [line]
                depth = line.count("(") - line.count(")")
                while depth > 0:
                    index += 1
                    chunk.append(block[index])
                    depth += block[index].count("(") - block[index].count(")")
                # A definition without a body is not parseable; the
                # signature is what is being read, so give it one.
                node = ast.parse("\n".join(chunk).rstrip() + ":\n    ...").body[0]
                assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
                found[node.name] = node
            index += 1
    return found


def _index_names(text: str) -> list[str]:
    """Every name the reference's index lists, in the order it lists them."""
    section = text.split("## Index of exported names", 1)[1]
    names: list[str] = []
    for paragraph in section.split("\n\n"):
        if paragraph.lstrip().startswith("**"):
            names += re.findall(r"`([^`]+)`", paragraph)
    return names


REFERENCE_TEXT = REFERENCE.read_text("utf-8")
INDEX = _index_names(REFERENCE_TEXT)
SIGNATURES = _reference_signatures(REFERENCE_TEXT)

#: The exported callables the reference states a signature for — one
#: check each, so a failure names the function rather than a count.
CHECKED = sorted(name for name in SIGNATURES if name in api.__all__)


def _stated_shape(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """The parameter names of a stated signature, ``*`` where it separates."""
    args = node.args
    shape = [parameter.arg for parameter in args.posonlyargs + args.args]
    if args.vararg:
        shape.append("*" + args.vararg.arg)
    elif args.kwonlyargs:
        shape.append("*")
    shape += [parameter.arg for parameter in args.kwonlyargs]
    if args.kwarg:
        shape.append("**" + args.kwarg.arg)
    return shape


def _real_shape(signature: inspect.Signature) -> list[str]:
    """The same reading of a live signature."""
    shape: list[str] = []
    separated = False
    for name, parameter in signature.parameters.items():
        if parameter.kind is parameter.VAR_POSITIONAL:
            shape.append("*" + name)
            separated = True
        elif parameter.kind is parameter.VAR_KEYWORD:
            shape.append("**" + name)
        else:
            if parameter.kind is parameter.KEYWORD_ONLY and not separated:
                shape.append("*")
                separated = True
            shape.append(name)
    return shape


def _stated_defaults(node: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr]:
    """The defaults a stated signature gives, by parameter name."""
    args = node.args
    stated: dict[str, ast.expr] = {}
    positional = args.posonlyargs + args.args
    for parameter, default in zip(
        positional[len(positional) - len(args.defaults) :], args.defaults, strict=True
    ):
        stated[parameter.arg] = default
    for parameter, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        if default is not None:
            stated[parameter.arg] = default
    return stated


def _default_matches(stated: ast.expr, real: Any) -> bool:
    """Whether a live default is the one the reference states.

    A literal is compared by value. An expression — a name the surface
    itself exports, such as ``OPTIONS`` or ``random_pairing`` — is
    compared against that exported object, so the document cannot name
    one default while the code uses another. Anything else the reference
    may spell is only required to *have* a default, because this test
    reads a document and must not evaluate arbitrary text out of it.
    """
    if isinstance(stated, ast.Constant):
        return bool(real == stated.value)
    if isinstance(stated, ast.Tuple) and not stated.elts:
        return real == ()
    if isinstance(stated, ast.Name) and stated.id in api.__all__:
        return real is getattr(api, stated.id)
    return True


def test_the_index_and_the_surface_are_the_same_list() -> None:
    """``__all__`` is the index, minus what a later feature still owes.

    Both directions matter. A name in the index that nothing exports is a
    promise the package does not keep; a name exported without the index
    knowing it is a surface nobody documented, and the first consumer to
    find it turns it into one we have to keep.
    """
    expected = set(INDEX) - set(PENDING)
    exported = set(api.__all__)
    assert exported - expected == set(), "exported without being in the reference's index"
    assert expected - exported == set(), "in the reference's index and not exported"


def test_no_pending_name_is_exported() -> None:
    """A name is either owed or delivered, never both.

    Separate from the equality above so that a change which adds a name
    and forgets to strike it from :data:`PENDING` fails with the reason
    rather than with a set difference.
    """
    exported_but_pending = sorted(set(api.__all__) & set(PENDING))
    assert not exported_but_pending, (
        f"{exported_but_pending} are exported and still listed as pending — "
        "strike them from PENDING in the commit that exports them"
    )


def test_every_pending_name_is_in_the_index() -> None:
    """Nothing is owed that the reference does not ask for."""
    unknown = sorted(set(PENDING) - set(INDEX))
    assert not unknown, f"{unknown} are pending but in no group of the reference's index"


def test_the_index_lists_every_name_once() -> None:
    """One name, one group — the index is a list, not a cross-reference."""
    seen: dict[str, int] = {}
    for name in INDEX:
        seen[name] = seen.get(name, 0) + 1
    assert [name for name, count in seen.items() if count > 1] == []


def test_every_exported_name_resolves() -> None:
    """``__all__`` is a promise; each name in it has to be reachable.

    ``from mcuhome.workbench.api import *`` raises on the first name that
    is not there, and an embedder's import of one name does the same.
    """
    for name in api.__all__:
        assert hasattr(api, name), f"{name} is exported and does not exist"


def test_the_surface_points_at_its_reference() -> None:
    """A caller who reads the module finds the document."""
    assert api.__doc__ is not None
    assert "docs/api.md" in api.__doc__


@pytest.mark.parametrize("name", CHECKED)
def test_the_signature_matches_the_reference(name: str) -> None:
    """Parameter names, order, keyword-only-ness and stated defaults.

    The reference is what a caller writes their call against, so a
    parameter renamed in the code and not in the document is a broken
    call for everyone who followed it. Types are not compared: the
    document states them for a reader, and a type that is wrong is a
    documentation defect this test cannot tell from a deliberate
    widening.
    """
    node = SIGNATURES[name]
    signature = inspect.signature(getattr(api, name))
    assert _real_shape(signature) == _stated_shape(node), (
        f"{name}: the reference states {_stated_shape(node)}, the code has {_real_shape(signature)}"
    )

    stated = _stated_defaults(node)
    for parameter_name, parameter in signature.parameters.items():
        has_default = parameter.default is not inspect.Parameter.empty
        assert has_default == (parameter_name in stated), (
            f"{name}: the reference gives {parameter_name} "
            f"{'a' if parameter_name in stated else 'no'} default and the code "
            f"{'does' if has_default else 'does not'}"
        )
        if has_default:
            assert _default_matches(stated[parameter_name], parameter.default), (
                f"{name}: the reference defaults {parameter_name} to "
                f"{ast.unparse(stated[parameter_name])}, the code to {parameter.default!r}"
            )


def test_the_reference_states_a_signature_for_the_callables_it_documents() -> None:
    """The check above is only worth as much as its coverage.

    Every exported callable that is a function — classes state their
    fields in prose, and a re-exported constant is not one — must have a
    signature in the reference, or this file would pass by checking
    nothing.
    """
    unstated = sorted(
        name
        for name in api.__all__
        if inspect.isfunction(getattr(api, name)) or inspect.iscoroutinefunction(getattr(api, name))
        if name not in SIGNATURES
    )
    assert not unstated, f"{unstated} are exported functions the reference states no signature for"


def test_the_reference_is_read_from_this_repository() -> None:
    """The document under test is the one this checkout ships."""
    assert REFERENCE.is_file()
    assert len(INDEX) > 200  # noqa: PLR2004 - the surface is large by construction
    assert len(CHECKED) > 50  # noqa: PLR2004 - and most of it is callables


# --------------------------------------------------------------------------
# The seam table: what the suite reaches past the surface for
# --------------------------------------------------------------------------
#
# ``docs/api.md`` states, under "What is not public", which names MCUHome's
# own tests reach for behind ``api`` and why. A list like that is true on
# the day it is written and quietly false a sub-step later — a module
# acquires a test file, a module loses its last caller, and nobody notices
# because nothing is checked. So the suite's own imports are read out of
# the test tree and held against the table in both directions.
#
# What is compared, and what is not: the *names* are, the *reasons* are
# not. Whether a test is right to reach for something is a judgement, and
# the second column carries it for a reviewer. One part of that judgement
# is mechanical, though, and is checked below: a module that is imported
# only for names ``api`` exports is not a seam at all, it is an import to
# fix.
#
# What this cannot see: an import spelled inside a string — the source a
# test hands to a peer process it starts — and an attribute reached
# through a module that was imported as a whole. The first is read by
# nothing but the interpreter that runs it; the second is not a list an
# import statement carries.

SEAMS = REPO_ROOT / "tests" / "python"

#: The first cell of every row of the seam table, in order.
_SEAM_ROWS = [
    row.split("|")[1].strip()
    for row in REFERENCE_TEXT.split("### The seams MCUHome's own tests use", 1)[1]
    .split("\nTwo consequences worth stating:", 1)[0]
    .split("\n")
    if row.startswith("| ") and not row.startswith("|---")
][1:]  # the header row is not a seam


def _quoted(cell: str) -> list[str]:
    return re.findall(r"`([^`]+)`", cell)


def _table_modules() -> set[str]:
    """The module row's list: the row that says so, and only that one."""
    row = next(cell for cell in _SEAM_ROWS if cell.startswith("the modules behind the surface"))
    return set(_quoted(row))


def _table_attributes() -> list[tuple[str, str]]:
    """Every ``module.attribute`` the other rows name, as a pair."""
    found: list[tuple[str, str]] = []
    for cell in _SEAM_ROWS:
        if cell.startswith("the modules behind the surface"):
            continue
        for name in _quoted(cell):
            match = re.fullmatch(r"([a-z_][a-z0-9_]*)\.(\w+)", name)
            if match is not None:
                found.append((match.group(1), match.group(2)))
    return found


def _table_injections() -> list[tuple[str, tuple[str, ...]]]:
    """Every ``function(param=, …)`` the table names, with its parameters."""
    found: list[tuple[str, tuple[str, ...]]] = []
    for cell in _SEAM_ROWS:
        for name in _quoted(cell):
            match = re.fullmatch(r"(\w+)\((.*)\)", name)
            if match is not None:
                parameters = tuple(
                    part.strip().rstrip("=") for part in match.group(2).split(",") if part.strip()
                )
                found.append((match.group(1), parameters))
    return found


def _imported_modules() -> dict[str, set[str]]:
    """Which workbench modules the test tree imports, and from where.

    Every spelling an import can take — ``from mcuhome.workbench import
    x``, ``from mcuhome.workbench.x import y``, ``import
    mcuhome.workbench.x`` — because a rule that only sees one of them is
    a rule with a way around it.
    """
    found: dict[str, set[str]] = {}
    for source in sorted(SEAMS.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text("utf-8"))):
            reached: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module == "mcuhome.workbench":
                reached = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "mcuhome.workbench."
            ):
                reached = [node.module.split("mcuhome.workbench.", 1)[1]]  # type: ignore[union-attr]
            elif isinstance(node, ast.Import):
                reached = [
                    alias.name.split("mcuhome.workbench.", 1)[1]
                    for alias in node.names
                    if alias.name.startswith("mcuhome.workbench.")
                ]
            for module in reached:
                found.setdefault(module, set()).add(source.name)
    # `api` is the surface itself and `__version__` is the package's own
    # attribute; neither is a seam past anything.
    found.pop("api", None)
    found.pop("__version__", None)
    return found


def _imported_names() -> dict[str, set[str]]:
    """Which names the test tree takes out of each workbench module.

    Only the ``from mcuhome.workbench.x import y`` form: a module taken as
    a whole is reached for whatever the file later spells on it, and that
    is not a list an import statement carries.
    """
    found: dict[str, set[str]] = {}
    for source in sorted(SEAMS.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text("utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "mcuhome.workbench."
            ):
                module = node.module.split("mcuhome.workbench.", 1)[1]  # type: ignore[union-attr]
                found.setdefault(module, set()).update(alias.name for alias in node.names)
    return found


def _taken_as_a_whole() -> set[str]:
    """The modules a test file imports as a module rather than by name."""
    found: set[str] = set()
    for source in sorted(SEAMS.rglob("*.py")):
        for node in ast.walk(ast.parse(source.read_text("utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "mcuhome.workbench":
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                found.update(
                    alias.name.split("mcuhome.workbench.", 1)[1]
                    for alias in node.names
                    if alias.name.startswith("mcuhome.workbench.")
                )
    return found


def test_the_seam_table_names_every_module_the_suite_imports() -> None:
    """Both directions, because both are the same defect a day apart.

    A module the suite reaches for and the table does not carry is an
    undeclared seam — the document says the tests enter through `api`
    except for the reasons listed, and that is then untrue. A module the
    table carries and nothing imports is a seam that was removed and left
    standing in the document, which is how a list stops being read.
    """
    imported = _imported_modules()
    table = _table_modules()
    assert set(imported) - table == set(), "imported by the suite and not in the seam table"
    assert table - set(imported) == set(), "in the seam table and imported by nothing"


def test_every_module_the_seam_table_names_exists() -> None:
    """A renamed module leaves the table naming a file nobody has."""
    package = REPO_ROOT / "mcuhome" / "workbench"
    for module in sorted(_table_modules()):
        assert (package / f"{module}.py").is_file() or (package / module / "__init__.py").is_file()


def test_every_named_seam_resolves() -> None:
    """The attributes the table names, against the package.

    These are the rows that name one thing rather than a module: a
    constant a container double reads, a composition a test replaces, a
    private helper it patches. A row naming something that was renamed
    reads as a seam somebody still uses and is not one.
    """
    for module, attribute in _table_attributes():
        imported = importlib.import_module(f"mcuhome.workbench.{module}")
        assert hasattr(imported, attribute), f"{module}.{attribute} is in the table and not there"


def test_no_seam_the_table_names_has_an_exported_equivalent() -> None:
    """The checkable half of the rule the table's own text states.

    A row that names ``module.attribute`` for a name `api` exports is not
    a seam: every reader of the table would then be told to reach behind
    the surface for something that is on it. The rows that legitimately
    name an exported name are the injection ones, and those name a call
    with its parameters rather than an attribute, so they are a different
    shape and not read here.
    """
    named = [
        f"{module}.{attribute}"
        for module, attribute in _table_attributes()
        if attribute in api.__all__
    ]
    assert not named, f"{named} are named as seams and are exported — use the exported name"


def test_every_injection_parameter_the_table_names_is_in_the_signature() -> None:
    """The stated exceptions on exported functions are still parameters.

    They are the one place where a test is allowed to pass something the
    supported call does not mention, so the document names them. A
    parameter that went away leaves the document granting an exception
    for something that cannot be passed.
    """
    for name, parameters in _table_injections():
        assert name in api.__all__, f"{name} is named as an injection seam and is not exported"
        signature = inspect.signature(getattr(api, name))
        for parameter in parameters:
            assert parameter in signature.parameters, f"{name} takes no {parameter}"


def test_no_module_is_a_seam_only_for_a_name_the_surface_exports() -> None:
    """The checkable half of "use the exported one where there is one".

    A module the tests enter only to take names out of it that `api`
    exports is not a seam: every one of those imports can name `api`
    instead and the module leaves this list. Reaching *into* a module for
    its own internals is a different thing and is what the table's first
    row is about — so a module is only flagged when nothing is taken out
    of it but exported names, and no file takes it as a whole.
    """
    by_name = _imported_names()
    whole = _taken_as_a_whole()
    exported = set(api.__all__)
    avoidable = sorted(
        module for module, names in by_name.items() if module not in whole and names <= exported
    )
    assert not avoidable, (
        f"{avoidable} are imported only for names api exports — import them from api instead"
    )
