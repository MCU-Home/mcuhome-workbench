# tests/

Twister test suites (`testcase.yaml` per suite). Run from the workspace
top directory with:

```sh
west twister -T mcuhome/tests --integration --inline-logs -v
```

Host-run unit tests target `native_sim`. Empty until the first testable code
exists; CI is added together with the first test suite (we do not ship a red
pipeline).
