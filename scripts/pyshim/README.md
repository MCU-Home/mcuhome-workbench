# scripts/pyshim

CHIP v1.5.1.0's release tarball omits the `python_path` helper its codegen
scripts (`scripts/codegen.py`, `scripts/codegen_paths.py`) import to reach
`py_matter_idl` in the source tree — upstream's CI gets it from the
pigweed bootstrap environment, which a plain Zephyr build doesn't have
(upstream candidate C1). `python_path.py` here is a drop-in stand-in
providing the same `PythonPath` context manager.

Export `PYTHONPATH=<this directory>` before running CHIP codegen or
building Matter apps. The MCUHome builder container will bake this in so
device builds never need it set manually.
