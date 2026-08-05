# samples/

Sample applications demonstrating MCUHome features, each with a
`sample.yaml` so twister can build them (`west twister -T samples
--integration`).

| Sample | Demonstrates |
|---|---|
| `matter-node/` | Minimal Matter node on vanilla Zephyr with one dynamically registered temperature endpoint (upstream CHIP v1.5.1.0, nRF7002-DK) — moved from the workspace `matter-proto/` prototype after E2E commissioning verification. |
