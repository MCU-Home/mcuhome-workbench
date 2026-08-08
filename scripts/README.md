# scripts/

Development tooling and future custom west extension commands (registered
via `west-commands.yml` and the `self: west-commands:` key in
[west.yml](../west.yml) once the first command exists).

Current content:

| Entry | Purpose |
|---|---|
| `check_debug_output.py` | Lint against silently reduced diagnostics in config fragments (AGENTS.md "Debug output is load-bearing"); a reduction passes only with a `# debug-output: approved <reason>` marker. Runs in CI and as a pre-commit hook |
| `pyshim/` | Stand-in for CHIP's `python_path` helper missing from the v1.5.1.0 release tarball (upstream candidate C1) — see `pyshim/README.md` for the `PYTHONPATH` requirement |

