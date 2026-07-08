# actnow — Asynchronous RV32I Core (WIP)

An event-driven RISC-V (RV32I) core implemented in ACT (asynchronous/CHP). The
core boots into a low-power wait state, wakes on an external event, executes
straight-line instructions until it hits a custom `WFI` instruction, then
returns to waiting.

## Instructions to implement:
### Stores
- SB
- SH
- SW

Loads (LB/LH/LW/LBU/LHU) are implemented in `soc.act`, routed through the
MMU (`mmu.act`) to either internal RAM (`mem.act`) or external memory
(`addr_ext`/`mode_ext`/`wdata_ext`/`rdata_ext`), with sign/zero-extension
done in `soc.act` after the MMU's masking. See `tests/load_test.act`.

## Toolchain

Built and simulated against the `act`/`actsim` toolchain (`asyncvlsi/act`).
Requires `actsim` built from commit `fa1a636` ("tests and fixes for
user-defined enums") or later — earlier versions crash
(`Assertion: pos == nvals` in `state.cc`) the instant a `deftype` struct with
an enum-typed field (e.g. this project's `mode_mem_t`) is sent over a
channel, which `mmu.act`/`mem.act` do on every single memory transaction.
`tests/mode_mem_t_enum_bug_test.act` is a standalone regression test for
this. If your `actsim` predates the fix, update the `actsim` submodule
inside your `act`/`actflow` checkout to `origin/master` and rebuild just
`act` + `actsim` (no need to build the rest of an `actflow` monorepo
checkout, if you have one — the unrelated EDA tools in it aren't needed
here).

**Always compile and run from the project root (`actnow/`), never from
inside `tests/`.** ACT resolves every `import` path relative to the
compiler's working directory, not relative to the importing file — so
`soc.act`'s own `import "interrupt.act"` only resolves correctly if the
whole compilation runs with `actnow/` as the working directory:

```
cd actnow
aflat tests/<name>.act
actsim tests/<name>.act <defproc-name>
```

At the `actsim` prompt, type `cycle` to run to completion, `quit` to exit.
