# include/mcuhome/

Public C headers of the MCUHome runtime. Everything under this directory is
API that components and user code may rely on; internal headers stay next to
their sources.

| Header | API |
|---|---|
| `matter_tables.h` | The generated-tables contract (ADR 0014): the plain-C structs a builder-generated device configuration is made of. Zero CHIP types by design — this is what insulates generated code from Matter SDK churn. Versioned via `MCUHOME_MATTER_TABLES_VERSION`. |
| `matter.h` | The Matter runtime API: `mcuhome_matter_start()`, `mcuhome_matter_attr_changed()`, and the weak `mcuhome_matter_stage()` bring-up hook. |
| `channel.h` | The channel layer (component model §5): `struct mcuhome_channel` binds a typed value to one Matter attribute, `struct mcuhome_sensor_binding` binds a Zephyr sensor reading to a channel, `mcuhome_sensor_start()` starts the poller. Dumb data, like the tables — the builder will emit these arrays too. |
| `matter/chip_project_config.h` | Framework-owned CHIP SDK configuration, derived from Kconfig. Included by an application's one-line `CHIPProjectConfig.h` wrapper, not directly by application code. |
| `entropy_ipc.h` | Wire protocol of the netcore entropy service: the request/response framing shared by the application-core driver (`drivers/entropy/`) and the network-core image (`samples/netcore-radio/`). Plain C with no Zephyr dependency, because the two images share nothing else. |

Rule of thumb for anything added here: if it needs a CHIP, ember or other
SDK type in its signature, it does not belong in this directory —
application and generated code must stay compilable as plain C.
