# actnow — Asynchronous RV32I Core (WIP)

An event-driven RISC-V (RV32I) core implemented in ACT (asynchronous/CHP). The
core boots into a low-power wait state, wakes on an external event, executes
straight-line instructions until it hits a custom `WFI` instruction, then
returns to waiting.

## Instructions to implement:
### Loads
- LB
- LH
- LW
- LBU
- LHU
### Stores
- SB
- SH
- SW


## Toolchain

Built and simulated against the `act`/`actsim` toolchain (`asyncvlsi/act`).

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
