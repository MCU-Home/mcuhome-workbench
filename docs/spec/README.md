# Specifications (in the SDK repository)

The workbench is the orchestrator these documents are written against,
so they are listed here — they live in
[mcu-home/mcuhome-sdk](https://github.com/mcu-home/mcuhome-sdk),
next to the build environment they describe.

| Document | What it says |
|---|---|
| [build-environment-specification.md](https://github.com/mcu-home/mcuhome-sdk/blob/main/docs/spec/build-environment-specification.md) | What a build environment must do, and what it may rely on. Written for somebody building their own. |
| [build-context-format.md](https://github.com/mcu-home/mcuhome-sdk/blob/main/docs/spec/build-context-format.md) | What this workbench puts in a build context. `mcuhome.workbench.contextdir` writes it. |
| [build-actions.md](https://github.com/mcu-home/mcuhome-sdk/blob/main/docs/spec/build-actions.md) | Which actions this workbench sends, and what each one produces. |
