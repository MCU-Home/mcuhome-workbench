# samples/

Sample applications demonstrating MCUHome features, each with a
`sample.yaml` so twister can build them (`west twister -T samples
--integration`).

| Sample | Demonstrates |
|---|---|
| `matter-node/` | Native composed Matter node (ADR 0014) on vanilla Zephyr: framework root-only ZAP plus one temperature endpoint registered at runtime as EP1 under the root (upstream CHIP v1.5.1.0, nRF7002-DK). Requires the `matter` and `debug-rtt` snippets — see [matter-node/README.md](matter-node/README.md). |
