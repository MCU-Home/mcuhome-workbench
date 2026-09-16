# SPDX-FileCopyrightText: 2026 The MCUHome Contributors
# SPDX-License-Identifier: Apache-2.0
"""Stage 4 on this machine, and its optional edge (``generate.py``).

No build takes this path — a build environment generates from the model
its build context carries — so what is asserted here is the seam itself:
the one caller that wants the tree for its own sake reaches the compiler
through :func:`~mcuhome.workbench.generate.generate_application`, and where that
distribution is absent it gets a sentence naming the install rather than
a traceback.

The discriminator matters as much as the refusal: an ``ImportError``
raised from *inside* an installed compiler is not evidence of its absence,
and must travel on with its own name in it.
"""

from __future__ import annotations

import json

import pytest
from conftest import EXAMPLES_DIR, resolve_file

from mcuhome.workbench import generate


@pytest.fixture
def model():
    return resolve_file(EXAMPLES_DIR / "00-bmp180-two-endpoints.yaml")


def _failing_import(monkeypatch, error: ImportError) -> None:
    """Replace the one call ``_compiler`` makes, with a stated failure.

    ``importlib.import_module`` goes straight to the import machinery and
    never through ``builtins.__import__``, so this is the seam.
    """
    import importlib

    def refuse(name: str):
        raise error

    monkeypatch.setattr(importlib, "import_module", refuse)


def test_the_generated_tree_is_written_and_every_file_named(model, tmp_path) -> None:
    result = generate.generate_application(model, out_dir=tmp_path)
    assert result.files
    assert all(path.is_file() for path in result.files)
    assert all(path.is_relative_to(tmp_path) for path in result.files)


def test_the_generation_result_names_the_device_and_the_directory(model, tmp_path) -> None:
    """The document a client prints, rather than the paths alone.

    Which device the tree belongs to and where it went are facts of the
    act; a client that stated them itself would be repeating the
    arguments it passed in.
    """
    result = generate.generate_application(model, out_dir=tmp_path)

    assert result.device == model.device.name
    assert result.out_dir == tmp_path
    document = result.to_dict()
    assert sorted(document) == ["device", "files", "out_dir"]
    assert document["out_dir"] == str(tmp_path)
    assert document["files"] == [str(path) for path in result.files]
    assert json.dumps(document)


def test_a_missing_compiler_distribution_is_the_named_refusal(model, tmp_path, monkeypatch):
    """The compiler is optional, so its absence is a sentence."""
    _failing_import(
        monkeypatch,
        ModuleNotFoundError("No module named 'mcuhome.compiler'", name="mcuhome.compiler"),
    )
    with pytest.raises(generate.CompilerUnavailable) as refusal:
        generate.generate_application(model, out_dir=tmp_path)
    assert "pip install mcuhome-compiler" in str(refusal.value.hint)


def test_a_broken_dependency_inside_an_installed_compiler_surfaces_unchanged(
    model, tmp_path, monkeypatch
):
    """An ImportError from *within* the compiler is not evidence of its absence.

    A wheel built for another interpreter fails the same ``import_module``
    call, and translating that into "mcuhome-compiler is not installed"
    would send the reader to reinstall the one piece that is already
    there. The discriminator is the failing import's own name.
    """
    broken = ImportError("libzstd.so.1: cannot open shared object file", name="zstandard")
    _failing_import(monkeypatch, broken)
    with pytest.raises(ImportError) as raised:
        generate.generate_application(model, out_dir=tmp_path)
    assert raised.value is broken
    assert not isinstance(raised.value, generate.CompilerUnavailable)
