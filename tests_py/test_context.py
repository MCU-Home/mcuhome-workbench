# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""The build context format and its normative ID.

The model half of the subject: :mod:`mcuhome.model.context` is the
format, the canonical encoding and the ID rule — the vocabulary a build
server recomputes an ID with while carrying no build logic at all (ADR
0020 decision 4). The directory that rule is applied to is
:mod:`mcuhome.workbench.contextdir`, and it is tested next door in
``test_context_workbench.py``.

The context ID rule (build-container-contract.md §3.3, ADR 0018) is
locked with ``context`` format version 1 and can never change
afterwards — every archived context, every artifact attribution and
every server-side integrity check depends on the same inputs hashing to
the same ID forever. That makes :data:`GOLDEN_ID` the contract of this
file, not a regression convenience: if it ever fails, the fix is in the
code, never in the constant.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from mcuhome.model.context import (
    BACKEND_DIR,
    CONTEXT_FILE,
    CONTEXT_ID_VECTORS,
    CONTEXT_VERSION,
    MANIFEST_FILE,
    ContextFile,
    canonical_json,
    context_id,
    validate_zephyr_line,
    vector_id,
)
from mcuhome.model.errors import BuildError
from mcuhome.model.toolchain import line_of, normalize_release, satisfies_line

# The fixed synthetic inputs of the golden vector.
SDK_SHA = "cd" * 32
BOARD = "nrf7002dk/nrf5340/cpuapp"
FILES = (
    ContextFile(path="model/device-model.json", sha256="11" * 32),
    ContextFile(path="patches/zephyr/0001-fix.patch", sha256="22" * 32),
)

#: The golden vector: the inputs above hash to exactly this ID, in this
#: builder and in every builder that will ever exist. NEVER update this
#: constant — a change here is a change to a frozen contract, and the
#: bug is in the code that made it necessary.
#:
#: It moved exactly once, with the bump to context format 2 (E61), which
#: is the only thing that may move it: the hashed document lost its
#: ``container`` member, so the same inputs hash to a different number
#: under a different format version. A frozen rule is frozen per format
#: version, and version 1 no longer exists to disagree with this.
GOLDEN_ID = "sha256:11affc97a03519f03745793f96c02d40d8b0216b0c57772d94cec5f322f8abcf"


# --------------------------------------------------------------------------
# Canonical JSON — the encoding under the hash
# --------------------------------------------------------------------------


def test_canonical_json_sorts_keys_and_uses_minimal_separators() -> None:
    value = {"b": "1", "a": {"d": "2", "c": "3"}, "list": ["x", "y"]}
    assert canonical_json(value) == '{"a":{"c":"3","d":"2"},"b":"1","list":["x","y"]}'


def test_canonical_json_emits_non_ascii_literally() -> None:
    """RFC 8785 forbids \\u escapes for characters that need none."""
    assert canonical_json({"board": "nrf–ü"}) == '{"board":"nrf–ü"}'
    assert canonical_json({"a": "ü"}).encode("utf-8") == b'{"a":"\xc3\xbc"}'


def test_canonical_json_escapes_strings_the_ecmascript_way() -> None:
    """Two-character escapes where they exist, lowercase \\u00xx otherwise."""
    assert canonical_json({"a": 'x"\\\n\t\x1f'}) == '{"a":"x\\"\\\\\\n\\t\\u001f"}'


# --------------------------------------------------------------------------
# The context ID — the normative rule, frozen
# --------------------------------------------------------------------------


def test_the_golden_vector_never_changes() -> None:
    """The regression anchor of the whole format. See GOLDEN_ID."""
    computed = context_id(sdk_sha256=SDK_SHA, board=BOARD, files=FILES)
    assert computed == GOLDEN_ID


@pytest.mark.parametrize("vector", CONTEXT_ID_VECTORS, ids=lambda vector: vector["name"])
def test_the_conformance_vectors_hold(vector) -> None:
    """The suite a *second* implementation is checked against.

    The golden vector above proves this builder does not drift. It cannot
    prove anything about the build server, which ADR 0019 §8 obliges to
    recompute the same ID from the bytes it received — and which, per ADR
    0020 decision 4, is entitled to do so with nothing but the model
    package. :data:`CONTEXT_ID_VECTORS` ships inside that package so the
    obligation is checkable rather than asserted, and this test is the
    Python side running it.

    A failure here is never fixed in the data: version 2's vectors are
    frozen exactly as :data:`GOLDEN_ID` is.
    """
    assert vector_id(vector) == vector["id"]


def test_the_golden_vector_is_one_of_the_conformance_vectors() -> None:
    """Two frozen constants that disagree would be worse than one.

    The vector this file has pinned since the format was written is in
    the package's own suite, so a second implementation is checked
    against the same value the test suite is — not a second one that
    happens to look like it.
    """
    assert GOLDEN_ID in {vector["id"] for vector in CONTEXT_ID_VECTORS}


def test_an_implementation_sorting_by_utf16_code_units_fails_the_suite() -> None:
    """The suite's job, checked against the mistake it exists to catch.

    The hashed document *is* RFC 8785, and RFC 8785 orders object keys by
    UTF-16 code units. An implementation that reached for its JCS
    library's comparator for the ``files`` array would sort a different
    way — the two orders agree across the whole BMP and disagree the
    moment an astral path meets a BMP one — and would then compute a
    different context ID forever, for a context nobody could tell apart
    from a correct one.

    So: replay every vector with a UTF-16 sort in place of the code-point
    sort, and *some* vector must come out wrong. Five of the six do not
    (they are ASCII, or single-file, or below U+D800); the sixth is
    there so this assertion has something to stand on. Deleting it makes
    this test fail, which is the whole point of writing it as a check on
    the table rather than as a sixth assertion inside it.
    """

    def utf16_id(vector: dict) -> str:
        inputs = vector["inputs"]
        document = {
            "files": [
                {"path": path, "sha256": sha256}
                for path, sha256 in sorted(
                    inputs["files"], key=lambda entry: entry[0].encode("utf-16-be")
                )
            ],
            "sdk": {"sha256": inputs["sdk_sha256"]},
            "target": {"board": inputs["board"]},
        }
        return "sha256:" + hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()

    wrong = [vector["name"] for vector in CONTEXT_ID_VECTORS if utf16_id(vector) != vector["id"]]
    assert wrong, "no vector distinguishes code-point order from UTF-16 code-unit order"


def test_the_golden_vectors_canonical_form_never_changes() -> None:
    """The exact bytes under the hash, spelled out — nesting, order, all."""
    expected = (
        '{"files":['
        '{"path":"model/device-model.json","sha256":"' + "11" * 32 + '"},'
        '{"path":"patches/zephyr/0001-fix.patch","sha256":"' + "22" * 32 + '"}],'
        '"sdk":{"sha256":"' + SDK_SHA + '"},'
        '"target":{"board":"' + BOARD + '"}}'
    )
    assert "sha256:" + hashlib.sha256(expected.encode("utf-8")).hexdigest() == GOLDEN_ID


def test_the_order_files_are_given_in_does_not_matter() -> None:
    """The sort is part of the rule: the list is a set with an encoding."""
    computed = context_id(sdk_sha256=SDK_SHA, board=BOARD, files=reversed(FILES))
    assert computed == GOLDEN_ID


def test_every_hashed_field_changes_the_id() -> None:
    variants = [
        {"sdk_sha256": "dc" * 32},
        {"board": "nrf52840dk/nrf52840"},
        # A file's content, a file's path, one file more, one file less.
        {"files": (FILES[0], replace(FILES[1], sha256="33" * 32))},
        {"files": (FILES[0], replace(FILES[1], path="patches/sdk/0001-fix.patch"))},
        {"files": (*FILES, ContextFile(path="patches/sdk/0001-more.patch", sha256="44" * 32))},
        {"files": FILES[:1]},
    ]
    ids = {
        context_id(
            **{
                "sdk_sha256": SDK_SHA,
                "board": BOARD,
                "files": FILES,
                **variant,
            }
        )
        for variant in variants
    }
    assert GOLDEN_ID not in ids
    assert len(ids) == len(variants), "two different inputs collided on one ID"


def test_a_duplicate_path_is_refused() -> None:
    with pytest.raises(BuildError) as caught:
        context_id(
            sdk_sha256=SDK_SHA,
            board=BOARD,
            files=(FILES[0], replace(FILES[0], sha256="33" * 32)),
        )
    assert "twice" in caught.value.message


@pytest.mark.parametrize("line", ["", "  ", "v4.4", "4.4.0-rc1", "latest", "4.x"])
def test_a_malformed_zephyr_line_is_refused(line: str) -> None:
    """A line is dotted decimals: no leading v, no suffix, no word."""
    with pytest.raises(BuildError):
        validate_zephyr_line(line)


def test_a_malformed_file_hash_is_refused() -> None:
    with pytest.raises(BuildError):
        context_id(
            sdk_sha256=SDK_SHA,
            board=BOARD,
            files=(replace(FILES[0], sha256="not-a-hash"),),
        )


def test_a_missing_board_is_refused() -> None:
    with pytest.raises(BuildError):
        context_id(sdk_sha256=SDK_SHA, board="  ", files=FILES)


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "patches/../escape.patch", "patches\\zephyr\\0001.patch", "", "a//b"],
)
def test_an_unusable_path_is_refused(path: str) -> None:
    with pytest.raises(BuildError):
        context_id(
            sdk_sha256=SDK_SHA,
            board=BOARD,
            files=(ContextFile(path=path, sha256="11" * 32),),
        )


@pytest.mark.parametrize("path", [MANIFEST_FILE, CONTEXT_FILE, f"{BACKEND_DIR}/command.json"])
def test_the_integrity_list_may_not_name_what_is_not_content(path: str) -> None:
    """Both context documents and the backend directory stay out of the ID.

    ``context.yaml`` is the one that went unenforced for a while: §3.2
    excludes it "as a statement about the hash rather than about layout"
    — its never-hashed fields (constraint, url, zephyr, created) would leak
    into an identity §6 computes from resolved values alone — but the
    shared vocabulary accepted it anyway, leaving the exclusion to every
    caller separately. The build server recomputes IDs from received
    bytes (ADR 0019 §8), so the one implementation both sides share must
    be the place that refuses.
    """
    with pytest.raises(BuildError) as caught:
        context_id(
            sdk_sha256=SDK_SHA,
            board=BOARD,
            files=(ContextFile(path=path, sha256="11" * 32),),
        )
    assert "must not name" in caught.value.message


# --------------------------------------------------------------------------
# Context format 2: the requirement, and the backend's answer to it (E61)
# --------------------------------------------------------------------------


def test_the_format_version_is_two() -> None:
    """Version 1 is gone rather than supported alongside this one.

    Pinned as a number because everything else in this file is written
    against it: the golden ID, the vectors, and the refusal a document of
    another version gets. Nothing is published, so the bump cost nothing
    — and this assertion is what makes the next bump a deliberate act.
    """
    assert CONTEXT_VERSION == 2


@pytest.mark.parametrize(
    ("version", "line", "expected"),
    [
        # A line is a prefix, component by component.
        ("4.4", "4.4", True),
        ("4.4.0", "4.4", True),
        ("4.4.12", "4.4", True),
        ("4.4.0", "4", True),
        ("4.4.0", "4.4.0", True),
        # …and only component by component: 4.40 merely starts with the
        # same digits, and a longer line is not satisfied by a shorter
        # release.
        ("4.40.0", "4.4", False),
        ("4.5.0", "4.4", False),
        ("4.4", "4.4.0", False),
        # A pre-release satisfies no line, including its own (§2.1.1:
        # "not ordered at all" — and a line is a range).
        ("4.5.0-rc1", "4.5", False),
        ("4.5.0-rc1", "4.5.0", False),
        # Absence, and nonsense, are never read as compatible.
        ("", "4.4", False),
        ("v4.4.0", "4.4", False),
        ("latest", "4.4", False),
        ("4.4.0", "", False),
    ],
)
def test_which_container_serves_a_line(version: str, line: str, expected: bool) -> None:
    """The match both backends make, in ``mcuhome-model`` so they make one.

    The local build method asks it of the image on a developer's host and
    the build server asks it of every image in its inventory; two
    spellings of "this container serves 4.4" is how the two start
    disagreeing about one container.
    """
    assert satisfies_line(version, line=line) is expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("4.4", "4.4"),
        ("4.4.0", "4.4"),
        ("4.4.12", "4.4"),
        ("4.5.0", "4.5"),
        # A one-component release is its own line; there is no second
        # component to reduce to.
        ("4", "4"),
        # A pre-release serves no line, so it *is* no line — which is the
        # whole reason a backend must not report one as "available".
        ("4.5.0-rc1", None),
        # …and neither is anything that is not a release at all.
        ("", None),
        ("v4.4.0", None),
        ("latest", None),
    ],
)
def test_the_line_a_release_belongs_to(version: str, expected: str | None) -> None:
    """``satisfies_line``'s inverse, for telling a client what is served.

    A backend that cannot answer a context reports what it *could*
    answer, and both ADR 0019 and the build server's error registry call
    those values "the lines available" — while the values they are read
    off, ``org.mcuhome.zephyr`` labels, are releases. The reduction lives
    beside the match so that "serves 4.4" and "offers 4.4" cannot drift
    apart.
    """
    assert line_of(version) == expected


@pytest.mark.parametrize("version", ["4.4", "4.4.0", "4.4.12", "4", "4.5.0"])
def test_a_reported_line_is_always_one_the_release_satisfies(version: str) -> None:
    """The invariant that makes the reduction usable rather than merely
    tidy: a client that echoes back a reported line gets an image."""
    line = line_of(version)
    assert line is not None
    assert satisfies_line(version, line=line)


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        # West's own spelling: the one case this exists for.
        ("v4.4.0", "4.4.0"),
        # No leading v — nothing to strip, unchanged.
        ("4.4.0", "4.4.0"),
        # Exactly one v, however many there are: not west's doing beyond
        # the first, and this is not a loop.
        ("vv4.4.0", "v4.4.0"),
        # The whole string can be the v.
        ("v", ""),
        # Absence stays absence.
        ("", ""),
    ],
)
def test_normalize_release_strips_one_leading_v(version: str, expected: str) -> None:
    """West states every pinned revision with a ``v`` no release grammar
    carries; this is the one place that strip happens, for every reader
    of a ``describe`` answer or a west revision alike."""
    assert normalize_release(version) == expected
