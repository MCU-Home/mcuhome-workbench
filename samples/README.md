# samples/

Sample applications demonstrating MCUHome features, each with a
`sample.yaml` so twister can build them (`west twister -T samples
--integration`).

| Sample | Demonstrates |
|---|---|
| `matter-node/` | Native composed Matter node (ADR 0014) on vanilla Zephyr: framework root-only ZAP plus two runtime-registered endpoints under the root — EP1 temperature (0x0302) and EP2 pressure (0x0305), fed by a BMP180 (upstream CHIP v1.5.1.0, nRF7002-DK). Requires the `matter` and `debug-rtt` snippets — see [matter-node/README.md](matter-node/README.md). |
| `netcore-radio/` | The other half of an nRF5340 node: the network-core image. Everything the upstream `802154_rpmsg` sample does, plus the MCUHome entropy service that gives the application core access to this core's RNG. Replaces the upstream image outright — see [netcore-radio/README.md](netcore-radio/README.md) for the two-image build and flash sequence. |
