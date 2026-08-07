# scripts/

Development tooling and future custom west extension commands (registered
via `west-commands.yml` and the `self: west-commands:` key in
[west.yml](../west.yml) once the first command exists).

Current content:

| Entry | Purpose |
|---|---|
| `pyshim/` | Stand-in for CHIP's `python_path` helper missing from the v1.5.1.0 release tarball (upstream candidate C1) — see `pyshim/README.md` for the `PYTHONPATH` requirement |

